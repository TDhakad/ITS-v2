from __future__ import annotations

import asyncio
import importlib
import json
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import (
    admin_reset_password,
    change_password,
    get_current_user,
    get_optional_user,
    login,
    logout,
    register,
    require_role,
)
from app.db import add_chat_turn_messages, add_project_member, create_project
from app.db import create_ticket_comment as persist_ticket_comment
from app.db import create_ticket as persist_ticket
from app.db import (
    delete_chat_thread_messages,
    delete_ticket_comment,
    enqueue_background_job,
    get_cached_ticket_insight,
    get_kb_project_ids,
    get_project_by_id,
    get_session,
    get_ticket,
    get_user_by_id,
    get_user_project_ids,
    init_db,
    list_chat_messages,
    list_chat_threads,
    list_project_members,
    list_projects,
    list_tags,
    list_ticket_comments,
    list_tickets,
    list_users,
    remove_project_member,
    save_ticket_insight,
    SessionLocal,
    update_ticket_comment,
)
from app.llm import get_chat_model
from app.schemas import (
    Environment,
    KBArticleRef,
    Priority,
    ProjectAccessLevel,
    ProjectCreate,
    ResolutionData,
    TicketCategory,
    TicketCreate,
    TicketIntelligence,
    TicketRead,
    TicketStatus,
    UserClearance,
    UserRead,
    UserRole,
)
from app.settings import get_settings
from app.ticket_insights import (
    INSIGHT_CACHE_TTL_SECONDS,
    build_ticket_insights_payload,
    ticket_insight_cache_key,
)

AdminRole = require_role(UserRole.ADMIN)
AdminUser = Annotated[UserRead, Depends(AdminRole)]
CurrentUser = Annotated[UserRead, Depends(get_current_user)]
OptionalUser = Annotated[UserRead | None, Depends(get_optional_user)]
DBSession = Annotated[Session, Depends(get_session)]

app = FastAPI(
    title="Capstone ITS v2",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_settings = get_settings()
if _settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"
FRONTEND_ASSETS = FRONTEND_DIST / "assets"
LEGACY_KB_DIR = Path(__file__).resolve().parent.parent / "data-v2" / "knowledge_base"
if FRONTEND_ASSETS.exists():
    app.mount(
        "/assets", StaticFiles(directory=str(FRONTEND_ASSETS)), name="frontend-assets"
    )
_db_initialized = False
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, max_length=120)
    thread_id: str | None = Field(default=None, max_length=120)
    # user_id / clearance are resolved from session.
    user_id: str = Field(default="anonymous", min_length=1, max_length=120)
    app_name: str | None = Field(default=None, max_length=120)
    environment: Environment = Environment.UNKNOWN
    clearance: UserClearance | None = None
    project_id: int | None = None


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    description: str = Field(default="", max_length=1_000)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class AdminResetPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    new_password: str = Field(min_length=8, max_length=128)


class AdminAnalyticsRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1_000)


class TicketCreateRequest(BaseModel):
    description: str = Field(min_length=1, max_length=8000)
    user_id: str = Field(default="anonymous", min_length=1, max_length=120)
    thread_id: str = Field(default="manual", min_length=1, max_length=120)
    project_id: int | None = None
    title: str | None = Field(default=None, max_length=300)
    app_name: str | None = Field(default=None, max_length=120)
    environment: Environment = Environment.UNKNOWN
    clearance: UserClearance = UserClearance.PUBLIC
    category: TicketCategory = TicketCategory.INFRA
    priority: Priority = Priority.MEDIUM
    keywords: list[str] = Field(default_factory=list, max_length=12)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TicketCommentCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4_000)
    parent_comment_id: int | None = Field(default=None, ge=1)


class TicketCommentUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4_000)


@app.on_event("startup")
def startup() -> None:
    settings = _settings
    settings.ensure_local_dirs()
    settings.configure_langsmith_environment()
    _ensure_db()


# ── Admin-only Swagger docs ──────────────────────────────────────────────────


@app.get("/openapi.json", include_in_schema=False)
def openapi_schema(
    _: AdminUser,
) -> JSONResponse:
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def swagger_ui(
    _: AdminUser,
) -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="Capstone ITS v2 — API Docs",
        swagger_ui_parameters={"persistAuthorization": True},
    )


# ── Pages ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def chat_page(request: Request, current_user: OptionalUser) -> Response:
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return templates.TemplateResponse(request, "chat.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return templates.TemplateResponse(request, "login.html")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> Response:
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return templates.TemplateResponse(request, "register.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    current_user: OptionalUser,
) -> Response:
    # Auth checks MUST happen before serving the SPA so /admin stays protected.
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to perform this action.",
        )
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return templates.TemplateResponse(request, "admin.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/chat/threads")
def chat_threads(
    db: DBSession,
    current_user: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    resolved_user_id = str(current_user.id)
    threads = list_chat_threads(db, user_id=resolved_user_id)
    return {"threads": threads}


@app.get("/api/chat/history")
def chat_history(
    db: DBSession,
    current_user: CurrentUser,
    thread_id: str | None = Query(default=None, max_length=120),
) -> dict[str, Any]:
    _ensure_db()
    resolved_user_id = str(current_user.id)
    resolved_thread_id = _resolve_chat_thread_id(resolved_user_id, thread_id)
    try:
        records = list_chat_messages(
            db,
            user_id=resolved_user_id,
            thread_id=resolved_thread_id,
        )
    except SQLAlchemyError as exc:
        logger.exception("Chat history unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat history unavailable.",
        ) from exc

    return {
        "conversation_id": resolved_thread_id,
        "thread_id": resolved_thread_id,
        "messages": [_chat_message_to_api(record) for record in records],
    }


@app.delete("/api/chat/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_thread(
    thread_id: str,
    db: DBSession,
    current_user: CurrentUser,
) -> Response:
    _ensure_db()
    resolved_user_id = str(current_user.id)
    resolved_thread_id = _resolve_chat_thread_id(resolved_user_id, thread_id)
    if resolved_thread_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat thread not found.",
        )
    try:
        deleted_count = delete_chat_thread_messages(
            db,
            user_id=resolved_user_id,
            thread_id=resolved_thread_id,
        )
    except SQLAlchemyError as exc:
        logger.exception("Chat thread delete unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat thread delete unavailable.",
        ) from exc

    if deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat thread not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/chat")
def chat_turn(
    payload: ChatRequest,
    request: Request,
    current_user: CurrentUser,
) -> dict[str, Any]:
    del request
    return _execute_chat_turn_payload(payload=payload, current_user=current_user)


@app.post("/api/chat/stream")
async def chat_turn_stream(
    payload: ChatRequest,
    request: Request,
    current_user: CurrentUser,
) -> StreamingResponse:
    del request
    _ensure_db()
    resolved_user_id = str(current_user.id)
    thread_id = _resolve_chat_thread_id(
        resolved_user_id,
        payload.thread_id or payload.conversation_id,
    )

    async def event_stream():
        yield _sse_event(
            "start",
            {
                "conversation_id": thread_id,
                "thread_id": thread_id,
            },
        )

        worker = asyncio.create_task(
            asyncio.to_thread(
                _execute_chat_turn_payload,
                payload=payload,
                current_user=current_user,
            )
        )

        # Thinking stream temporarily disabled.
        while not worker.done():
            await asyncio.sleep(0.04)

        try:
            response = await worker
        except HTTPException as exc:
            yield _sse_event(
                "error",
                {
                    "detail": str(exc.detail) or "Chat backend unavailable.",
                    "status": exc.status_code,
                },
            )
            yield _sse_event("done", {"ok": False})
            return
        except Exception:
            logger.exception("Chat stream backend unavailable")
            yield _sse_event(
                "error",
                {
                    "detail": "Chat backend unavailable. Check server logs for initialization details.",
                    "status": status.HTTP_503_SERVICE_UNAVAILABLE,
                },
            )
            yield _sse_event("done", {"ok": False})
            return

        response_text = str(response.get("response") or response.get("message") or "")
        for token in _chunk_stream_text(response_text, min_size=2, max_size=4):
            yield _sse_event("message_token", {"token": token})
            await asyncio.sleep(0.015)

        yield _sse_event("final", response)
        yield _sse_event("done", {"ok": True})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/tickets", status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreateRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    resolved_project_id = payload.project_id
    if current_user.role != UserRole.ADMIN:
        user_project_ids = get_user_project_ids(db, current_user.id)
        if not user_project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No project access assigned to this user.",
            )
        if payload.project_id and payload.project_id not in user_project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to the requested project.",
            )
        resolved_project_id = payload.project_id or user_project_ids[0]

    try:
        summary = payload.title or payload.description.strip().splitlines()[0][:120]
        ticket = persist_ticket(
            db,
            TicketCreate(
                status=TicketStatus.OPEN,
                user_id=str(current_user.id),
                thread_id=payload.thread_id,
                app_name=payload.app_name,
                environment=payload.environment,
                user_clearance=current_user.clearance,
                project_id=resolved_project_id,
                intelligence=TicketIntelligence(
                    category=payload.category,
                    suggested_priority=payload.priority,
                    summary=summary,
                    keywords=payload.keywords,
                    confidence=float(payload.metadata.get("confidence", 0.0) or 0.0),
                ),
                resolution=ResolutionData(),
                conversation=[{"role": "user", "content": payload.description}],
                raw_context={
                    "description": payload.description,
                    "metadata": payload.metadata,
                },
            ),
            index_vector=False,
        )
        enqueue_background_job(
            db,
            "ticket_vector_upsert",
            {"ticket_id": ticket.id},
        )
        enqueue_background_job(
            db,
            "ticket_insights_refresh",
            {"ticket_id": ticket.id},
        )
        return {"ticket": ticket_to_api(ticket, include_conversation=True)}
    except Exception as exc:
        db.rollback()
        logger.exception("Ticket persistence failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket persistence unavailable.",
        ) from exc


@app.get("/api/tickets")
def tickets(
    db: DBSession,
    current_user: CurrentUser,
    status_filter: str | None = Query(default=None, alias="status"),
    user_id: str | None = None,
    project_id: int | None = Query(default=None),
    tag_slug: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, list[dict[str, Any]]]:
    _ensure_db()
    parsed_status = _parse_status(status_filter)

    # Scope non-admin users to their project memberships so they never see
    # tickets that belong to other projects.
    effective_project_id: int | None = project_id
    effective_project_ids: list[int] | None = None
    if current_user.role != UserRole.ADMIN:
        user_project_ids = get_user_project_ids(db, current_user.id)
        if not user_project_ids:
            return {"tickets": []}
        if project_id and project_id not in user_project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to the requested project.",
            )
        if project_id and project_id in user_project_ids:
            effective_project_id = project_id
        elif len(user_project_ids) == 1:
            effective_project_id = user_project_ids[0]
        else:
            effective_project_ids = user_project_ids
            effective_project_id = None

    try:
        found = list_tickets(
            db,
            parsed_status,
            project_id=effective_project_id,
            project_ids=effective_project_ids,
            tag_slug=tag_slug,
            user_id=user_id,
            limit=limit,
        )
    except SQLAlchemyError as exc:
        logger.exception("Ticket listing failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket listing unavailable.",
        ) from exc
    project_names = _project_name_map(db)
    return {
        "tickets": [
            ticket_to_api(ticket, project_names=project_names) for ticket in found
        ]
    }


@app.get("/api/tickets/{ticket_id}")
def ticket_detail(
    ticket_id: int, db: DBSession, current_user: CurrentUser
) -> dict[str, Any]:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found"
        )
    _assert_ticket_access(db, current_user, ticket)
    return ticket_to_api(
        ticket, include_conversation=True, project_names=_project_name_map(db)
    )


@app.get("/api/tickets/{ticket_id}/comments")
def ticket_comments(
    ticket_id: int,
    db: DBSession,
    current_user: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )
    _assert_ticket_access(db, current_user, ticket)
    comments = list_ticket_comments(db, ticket_id)
    return {"comments": [_comment_to_api(comment) for comment in comments]}


@app.post("/api/tickets/{ticket_id}/comments", status_code=status.HTTP_201_CREATED)
def create_ticket_comment(
    ticket_id: int,
    payload: TicketCommentCreateRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )
    _assert_ticket_access(db, current_user, ticket)
    content = payload.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Comment content cannot be empty.",
        )

    try:
        comment = persist_ticket_comment(
            db,
            ticket_id=ticket_id,
            author_user_id=current_user.id,
            content=content,
            parent_comment_id=payload.parent_comment_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Ticket comment creation failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket comments unavailable.",
        ) from exc

    enqueue_background_job(
        db,
        "comment_vector_upsert",
        {"ticket_id": ticket_id, "comment_id": comment.id},
    )
    enqueue_background_job(
        db,
        "ticket_insights_refresh",
        {"ticket_id": ticket_id},
    )

    return {"comment": _comment_to_api(comment)}


@app.patch("/api/tickets/{ticket_id}/comments/{comment_id}")
def edit_ticket_comment(
    ticket_id: int,
    comment_id: int,
    payload: TicketCommentUpdateRequest,
    db: DBSession,
    _: AdminUser,
) -> dict[str, Any]:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    content = payload.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Comment content cannot be empty.",
        )

    try:
        comment = update_ticket_comment(
            db,
            ticket_id=ticket_id,
            comment_id=comment_id,
            content=content,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Ticket comment update failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket comments unavailable.",
        ) from exc

    if comment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found",
        )

    enqueue_background_job(
        db,
        "comment_vector_upsert",
        {"ticket_id": ticket_id, "comment_id": comment.id},
    )
    enqueue_background_job(
        db,
        "ticket_insights_refresh",
        {"ticket_id": ticket_id},
    )
    return {"comment": _comment_to_api(comment)}


@app.delete(
    "/api/tickets/{ticket_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_ticket_comment(
    ticket_id: int,
    comment_id: int,
    db: DBSession,
    _: AdminUser,
) -> Response:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    existing_comments = list_ticket_comments(db, ticket_id)
    comment_ids_to_delete = _comment_subtree_ids(existing_comments, comment_id)

    try:
        removed = delete_ticket_comment(
            db,
            ticket_id=ticket_id,
            comment_id=comment_id,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Ticket comment delete failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticket comments unavailable.",
        ) from exc

    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found",
        )

    if comment_ids_to_delete:
        enqueue_background_job(
            db,
            "comment_vector_delete",
            {"ticket_id": ticket_id, "comment_ids": comment_ids_to_delete},
        )
        enqueue_background_job(
            db,
            "ticket_insights_refresh",
            {"ticket_id": ticket_id},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/tickets/{ticket_id}/insights")
def ticket_insights(
    ticket_id: int, db: DBSession, current_user: CurrentUser
) -> dict[str, Any]:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found"
        )
    _assert_ticket_access(db, current_user, ticket)

    cache_key = ticket_insight_cache_key(ticket)
    cached = get_cached_ticket_insight(db, ticket_id=ticket.id, cache_key=cache_key)
    if cached is not None:
        return cached

    payload = build_ticket_insights_payload(ticket)
    save_ticket_insight(
        db,
        ticket_id=ticket.id,
        cache_key=cache_key,
        payload=payload,
        ttl_seconds=INSIGHT_CACHE_TTL_SECONDS,
    )
    return payload


@app.get("/api/admin/insights")
def admin_insights(
    db: DBSession,
    _: AdminUser,
) -> dict[str, Any]:
    _ensure_db()
    graph_available = True
    try:
        graph = importlib.import_module("app.graph")
        insights_func = getattr(graph, "admin_insights", None) or getattr(
            graph,
            "get_admin_insights",
            None,
        )
        if callable(insights_func):
            return {"available": True, "source": "app.graph", "data": insights_func()}
    except Exception:
        graph_available = False
        logger.exception("Graph admin insights unavailable; using database aggregate")

    try:
        ticket_payload = [
            ticket_to_api(ticket) for ticket in list_tickets(db, None, limit=500)
        ]
    except SQLAlchemyError as exc:
        logger.exception("Admin insight ticket aggregate failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin insights unavailable.",
        ) from exc

    by_status = _count_by(ticket_payload, "status")
    by_priority = _count_by(ticket_payload, "priority")
    by_category = _count_by(ticket_payload, "category")
    return {
        "available": True,
        "source": "app.db",
        "graph_available": graph_available,
        "data": {
            "total_tickets": len(ticket_payload),
            "by_status": by_status,
            "by_priority": by_priority,
            "by_category": by_category,
        },
    }


@app.post("/api/admin/analytics")
def admin_analytics(
    payload: AdminAnalyticsRequest,
    _: AdminUser,
) -> dict[str, Any]:
    _ensure_db()
    try:
        from app.admin_analytics import run_admin_analytics_question

        result = run_admin_analytics_question(payload.question)
    except Exception as exc:
        logger.exception("Admin analytics failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin analytics unavailable.",
        ) from exc
    return {"question": payload.question, **result}


# ── Auth routes ──────────────────────────────────────────────────────────────


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def auth_register(
    payload: RegisterRequest,
    db: DBSession,
) -> UserRead:
    _ensure_db()
    return register(db, payload.email, payload.display_name, payload.password)


@app.post("/auth/login")
def auth_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DBSession,
) -> UserRead:
    _ensure_db()
    user_agent = request.headers.get("user-agent", "")
    ip = request.client.host if request.client else ""
    user, token = login(
        db, payload.email, payload.password, user_agent=user_agent, ip_address=ip
    )
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind HTTPS in production
        max_age=86400,
    )
    return user


@app.post("/auth/logout")
def auth_logout(
    db: DBSession,
    session_token: str | None = Cookie(default=None),
) -> RedirectResponse:
    if session_token:
        logout(db, session_token)
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie("session_token")
    return redirect


@app.get("/auth/login")
def auth_login_redirect() -> RedirectResponse:
    """Redirect bare GET /auth/login to the login page."""
    return RedirectResponse(url="/login", status_code=301)


@app.get("/auth/me")
def auth_me(current_user: CurrentUser) -> UserRead:
    return current_user


@app.post("/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
def auth_change_password(
    payload: ChangePasswordRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> None:
    change_password(db, current_user.id, payload.current_password, payload.new_password)


@app.post("/auth/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def auth_reset_password(
    payload: AdminResetPasswordRequest,
    db: DBSession,
    _: AdminUser,
) -> None:
    admin_reset_password(db, payload.email, payload.new_password)


# ── Project routes ────────────────────────────────────────────────────────────


@app.get("/api/projects")
def get_projects(
    db: DBSession,
    current_user: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    records = list_projects(db)
    if current_user.role != UserRole.ADMIN:
        allowed_ids = set(get_user_project_ids(db, current_user.id))
        records = [record for record in records if record.id in allowed_ids]
    return {
        "projects": [
            {
                "id": r.id,
                "name": r.name,
                "slug": r.slug,
                "description": r.description,
                "owner_id": r.owner_id,
            }
            for r in records
        ]
    }


@app.post("/api/projects", status_code=status.HTTP_201_CREATED)
def post_project(
    payload: ProjectCreateRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    record = create_project(
        db,
        ProjectCreate(
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            owner_id=current_user.id,
        ),
    )
    add_project_member(db, record.id, current_user.id, ProjectAccessLevel.OWNER.value)
    return {"id": record.id, "name": record.name, "slug": record.slug}


class AddMemberRequest(BaseModel):
    user_id: int
    access_level: ProjectAccessLevel = ProjectAccessLevel.MEMBER


@app.get("/api/projects/{project_id}/members")
def get_project_members(
    project_id: int,
    db: DBSession,
    _: AdminUser,
) -> dict[str, Any]:
    _ensure_db()
    project = get_project_by_id(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found."
        )
    members = list_project_members(db, project_id)
    return {
        "project_id": project_id,
        "members": [
            {
                "user_id": m.user_id,
                "access_level": m.access_level,
                "display_name": m.user.display_name if m.user else None,
                "email": m.user.email if m.user else None,
            }
            for m in members
        ],
    }


@app.post("/api/projects/{project_id}/members", status_code=status.HTTP_201_CREATED)
def post_project_member(
    project_id: int,
    payload: AddMemberRequest,
    db: DBSession,
    _: AdminUser,
) -> dict[str, Any]:
    _ensure_db()
    project = get_project_by_id(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found."
        )
    target_user = get_user_by_id(db, payload.user_id)
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found."
        )
    member = add_project_member(
        db, project_id, payload.user_id, payload.access_level.value
    )
    return {
        "project_id": project_id,
        "user_id": member.user_id,
        "access_level": member.access_level,
    }


@app.delete(
    "/api/projects/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_project_member(
    project_id: int,
    user_id: int,
    db: DBSession,
    _: AdminUser,
) -> None:
    _ensure_db()
    removed = remove_project_member(db, project_id, user_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Membership not found.",
        )


@app.get("/api/users")
def get_users(
    db: DBSession,
    _: AdminUser,
) -> dict[str, Any]:
    _ensure_db()
    users = list_users(db)
    return {
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "role": u.role,
                "is_active": u.is_active,
            }
            for u in users
        ]
    }


# ── Tag routes ────────────────────────────────────────────────────────────────


@app.get("/api/tags")
def get_tags(db: DBSession) -> dict[str, Any]:
    _ensure_db()
    tags = list_tags(db)
    return {
        "tags": [
            {"id": t.id, "name": t.name, "slug": t.slug, "color": t.color} for t in tags
        ]
    }


@app.get("/api/kb/doc")
def get_kb_doc(
    current_user: CurrentUser,
    db: DBSession,
    source: str | None = Query(default=None, max_length=500),
    kb_id: str | None = Query(default=None, max_length=200),
) -> FileResponse:
    resolved = _resolve_kb_doc_path(source=source, kb_id=kb_id)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge article not found"
        )

    user_level = _clearance_level(current_user.clearance)
    article_level = _kb_doc_clearance_level(resolved)
    if user_level < article_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this knowledge article.",
        )
    linked_project_ids = _kb_doc_project_ids(db, resolved, kb_id)
    if linked_project_ids and current_user.role != UserRole.ADMIN:
        allowed_project_ids = set(get_user_project_ids(db, current_user.id))
        if not (linked_project_ids & allowed_project_ids):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this knowledge article.",
            )

    return FileResponse(
        path=resolved, media_type="text/markdown", filename=resolved.name
    )


@app.get("/{frontend_path:path}", include_in_schema=False)
def frontend_fallback(frontend_path: str, current_user: OptionalUser) -> Response:
    """Serve React client routes from the production build when available."""
    blocked_prefixes = ("api/", "auth/", "static/", "assets/")
    blocked_exact = {"api", "auth", "static", "assets", "docs", "openapi.json"}
    if frontend_path in blocked_exact or frontend_path.startswith(blocked_prefixes):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not FRONTEND_INDEX.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(FRONTEND_INDEX)


def _execute_chat_turn_payload(
    *,
    payload: ChatRequest,
    current_user: UserRead,
) -> dict[str, Any]:
    _ensure_db()
    resolved_user_id = str(current_user.id)
    resolved_clearance = current_user.clearance
    thread_id = _resolve_chat_thread_id(
        resolved_user_id,
        payload.thread_id or payload.conversation_id,
    )

    # Resolve project: auto-detect from the user's memberships for non-admin users
    # so the agent is always scoped to the correct project.
    resolved_project_id = payload.project_id
    resolved_project_ids: list[int] | None = None
    if current_user.role != UserRole.ADMIN:
        with SessionLocal() as db:
            user_project_ids = get_user_project_ids(db, current_user.id)
        if not user_project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No project access assigned to this user.",
            )
        resolved_project_ids = user_project_ids
        if payload.project_id and payload.project_id not in user_project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to the requested project.",
            )
        if payload.project_id and payload.project_id in user_project_ids:
            resolved_project_id = payload.project_id
        else:
            resolved_project_id = user_project_ids[0]

    try:
        graph = importlib.import_module("app.graph")
        runner = (
            getattr(graph, "run_chat_turn", None)
            or getattr(graph, "chat_turn", None)
            or getattr(graph, "invoke_triage", None)
        )
        if runner is None:
            raise RuntimeError(
                "app.graph does not expose run_chat_turn, chat_turn, or invoke_triage"
            )

        if getattr(runner, "__name__", "") == "invoke_triage":
            result = runner(payload.message, thread_id=thread_id)
        else:
            result = runner(
                thread_id=thread_id,
                user_id=resolved_user_id,
                message=payload.message,
                app_name=payload.app_name,
                environment=payload.environment,
                clearance=resolved_clearance,
                project_id=resolved_project_id,
                project_ids=resolved_project_ids,
                display_name=current_user.display_name if current_user else "User",
                user_role=current_user.role.value if current_user else "user",
            )
    except Exception as exc:
        logger.exception("Chat backend unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat backend unavailable. Check server logs for initialization details.",
        ) from exc

    ticket_id = _result_value(result, "ticket_id")
    linked_refs = _result_value(result, "linked_kb_articles", []) or []
    citations = [_kb_ref_to_api(ref) for ref in linked_refs]
    response_text = (
        _result_value(result, "response")
        or _result_value(result, "answer")
        or _result_value(result, "final_answer")
        or ""
    )

    response: dict[str, Any] = {
        "conversation_id": _result_value(result, "thread_id", thread_id),
        "thread_id": _result_value(result, "thread_id", thread_id),
        "message": response_text,
        "response": response_text,
        "route": _result_value(result, "route"),
        "ticket_id": ticket_id,
        "citations": citations,
        "references": citations,
    }
    agent_resp = _result_value(result, "agent_response")
    if agent_resp is not None:
        response["agent_response"] = (
            agent_resp.model_dump(mode="json")
            if hasattr(agent_resp, "model_dump")
            else agent_resp
        )

    with SessionLocal() as db:
        ticket = _safe_get_ticket(db, ticket_id) if ticket_id else None
        if ticket:
            response["ticket"] = ticket_to_api(ticket)

        try:
            agent_response_json: str | None = None
            if agent_resp is not None:
                agent_response_json = json.dumps(
                    agent_resp.model_dump(mode="json")
                    if hasattr(agent_resp, "model_dump")
                    else agent_resp
                )

            add_chat_turn_messages(
                db,
                user_id=resolved_user_id,
                thread_id=thread_id,
                user_message=payload.message,
                assistant_message=response_text,
                agent_response_json=agent_response_json,
            )
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Could not persist chat history for thread %s", thread_id)

    return response


def _build_reasoning_stream_text(message: str, project_id: int | None) -> str:
    compact = " ".join((message or "").split())
    if len(compact) > 180:
        compact = f"{compact[:180].rstrip()}..."
    project_context = (
        f"project {project_id}" if project_id is not None else "all accessible projects"
    )
    return (
        "Let me think this through step by step. "
        "I am mapping your request to ticket history, comment discussions, and knowledge references. "
        f"You asked: {compact}. "
        f"I will scope this to {project_context} and choose the best retrieval path before producing the final answer."
    )


def _chunk_stream_text(
    value: str,
    *,
    min_size: int,
    max_size: int,
):
    if not value:
        return
    chunk_size = max(min_size, 1)
    safe_max_size = max(max_size, chunk_size)
    index = 0
    length = len(value)
    toggle = False
    while index < length:
        current_size = safe_max_size if toggle else chunk_size
        toggle = not toggle
        yield value[index : index + current_size]
        index += current_size


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=True, default=str)}\n\n"


def _result_value(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _kb_search_roots() -> list[Path]:
    roots: list[Path] = []
    for candidate in (get_settings().kb_dir, LEGACY_KB_DIR):
        resolved = candidate.resolve()
        if resolved.exists() and resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _resolve_kb_doc_path(source: str | None, kb_id: str | None) -> Path | None:
    candidate_names: list[str] = []
    if source:
        candidate_names.append(source.strip().lstrip("/"))
    if kb_id:
        candidate_names.extend([f"{kb_id}.md", f"{kb_id}.markdown", kb_id])

    for root in _kb_search_roots():
        for candidate in candidate_names:
            resolved = _resolve_kb_candidate(root, candidate)
            if resolved is not None:
                return resolved
    return None


def _resolve_kb_candidate(root: Path, candidate: str) -> Path | None:
    cleaned = candidate.strip().replace("\\", "/")
    if not cleaned:
        return None
    candidate_path = Path(cleaned)
    if candidate_path.is_absolute():
        return None

    direct = (root / candidate_path).resolve()
    if _is_kb_doc(direct, root):
        return direct

    filename = candidate_path.name
    if not filename:
        return None
    for possible in root.rglob(filename):
        resolved = possible.resolve()
        if _is_kb_doc(resolved, root):
            return resolved
    return None


def _is_kb_doc(path: Path, root: Path) -> bool:
    if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
        return False
    if not path.is_file():
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _kb_doc_clearance_level(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return 0

    for line in lines[1:80]:
        stripped = line.strip()
        if stripped == "---":
            break
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        normalized_key = key.strip().lower()
        if normalized_key in {"clearance", "clearance_level"}:
            return _clearance_level(raw_value.strip())
    return 0


def _kb_doc_project_ids(db: Session, path: Path, kb_id: str | None) -> set[int]:
    project_ids: set[int] = set()
    for identifier in _kb_doc_identifiers(path, kb_id):
        project_ids.update(get_kb_project_ids(db, identifier))
    return project_ids


def _kb_doc_identifiers(path: Path, kb_id: str | None) -> list[str]:
    candidates = [kb_id, path.name, path.stem]
    for root in _kb_search_roots():
        try:
            relative = path.resolve().relative_to(root)
        except ValueError:
            continue
        candidates.extend([relative.as_posix(), relative.stem])

    identifiers: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        normalized = text.removesuffix(".md").removesuffix(".markdown")
        if normalized not in seen:
            seen.add(normalized)
            identifiers.append(normalized)
    return identifiers


def _clearance_level(value: Any) -> int:
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().lower()
    aliases = {"public": 0, "internal": 1, "restricted": 2}
    if text in aliases:
        return aliases[text]
    try:
        return int(text)
    except ValueError:
        return 0


def _ensure_db() -> None:
    global _db_initialized
    if _db_initialized:
        return
    get_settings().ensure_local_dirs()
    init_db()
    _db_initialized = True


def _safe_get_ticket(db: Session, ticket_id: Any) -> TicketRead | None:
    try:
        parsed_ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return None
    return get_ticket(db, parsed_ticket_id)


def _assert_ticket_access(
    db: Session, current_user: UserRead, ticket: TicketRead
) -> None:
    if current_user.role == UserRole.ADMIN:
        return
    allowed_project_ids = set(get_user_project_ids(db, current_user.id))
    if ticket.project_id is None or ticket.project_id not in allowed_project_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found"
        )


def _resolve_chat_thread_id(
    user_id: str, requested_thread_id: str | None = None
) -> str:
    prefix = f"user:{user_id}:"
    if requested_thread_id and requested_thread_id.startswith(prefix):
        return requested_thread_id
    return f"{prefix}default"


def _chat_message_to_api(message: Any) -> dict[str, Any]:
    import json as _json

    result: dict[str, Any] = {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }
    if message.agent_response:
        try:
            result["agent_response"] = _json.loads(message.agent_response)
        except Exception:
            pass
    return result


def _comment_to_api(comment: Any) -> dict[str, Any]:
    author = getattr(comment, "author", None)
    author_display_name = (
        getattr(author, "display_name", None)
        or getattr(author, "email", None)
        or f"User #{comment.author_user_id}"
    )
    author_role = getattr(author, "role", UserRole.USER.value)
    created_at = getattr(comment, "created_at", None)
    updated_at = getattr(comment, "updated_at", None)
    return {
        "id": comment.id,
        "ticket_id": comment.ticket_id,
        "parent_comment_id": comment.parent_comment_id,
        "author_user_id": comment.author_user_id,
        "author_display_name": author_display_name,
        "author_role": author_role,
        "content": comment.content,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "edited": bool(created_at and updated_at and updated_at > created_at),
    }


def _comment_subtree_ids(comments: list[Any], root_comment_id: int) -> list[int]:
    children_by_parent: dict[int, list[int]] = {}
    for comment in comments:
        parent_id = getattr(comment, "parent_comment_id", None)
        if parent_id is None:
            continue
        children_by_parent.setdefault(int(parent_id), []).append(int(comment.id))

    if not any(getattr(comment, "id", None) == root_comment_id for comment in comments):
        return []

    collected: list[int] = []
    queue = [root_comment_id]
    while queue:
        current_id = queue.pop(0)
        collected.append(current_id)
        queue.extend(children_by_parent.get(current_id, []))
    return collected


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _project_name_map(db: Session) -> dict[int, str]:
    """Return a {project_id: project_name} dict for all active projects."""
    return {p.id: p.name for p in list_projects(db, active_only=False)}


def ticket_to_api(
    ticket: TicketRead,
    include_conversation: bool = False,
    project_names: dict[int, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ticket.id,
        "ticket_id": ticket.id,
        "title": ticket.intelligence.summary[:96],
        "summary": ticket.intelligence.summary,
        "description": ticket.intelligence.summary,
        "requester": ticket.user_id,
        "created_by": ticket.user_id,
        "user_id": ticket.user_id,
        "thread_id": ticket.thread_id,
        "status": _status_to_api(ticket.status),
        "state": _status_to_api(ticket.status),
        "priority": ticket.intelligence.suggested_priority.value.lower(),
        "severity": ticket.intelligence.suggested_priority.value.lower(),
        "category": ticket.intelligence.category.value,
        "type": ticket.intelligence.category.value,
        "keywords": ticket.intelligence.keywords,
        "app_name": ticket.app_name,
        "environment": ticket.environment.value,
        "project_id": ticket.project_id,
        "project_name": (
            (project_names or {}).get(ticket.project_id) if ticket.project_id else None
        ),
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "linked_kb_articles": [],
        "duplicate_ticket_ids": [],
    }
    if include_conversation:
        payload["conversation"] = [
            message.model_dump(mode="json") for message in ticket.conversation
        ]
        payload["messages"] = payload["conversation"]
        payload["guardrail"] = (
            ticket.guardrail.model_dump(mode="json") if ticket.guardrail else None
        )
        payload["raw_context"] = ticket.raw_context
    return payload


def _kb_ref_to_api(ref: KBArticleRef) -> dict[str, Any]:
    return {
        "kb_id": ref.kb_id,
        "title": ref.title,
        "source": ref.source,
        "score": ref.relevance_score,
        "relevance_score": ref.relevance_score,
        "clearance": ref.clearance.value,
        "summary": f"{ref.title} ({ref.clearance.value})",
    }


def _parse_status(value: str | None) -> TicketStatus | None:
    if not value:
        return None
    normalized = value.casefold().replace("_", " ").strip()
    for status_value in TicketStatus:
        if status_value.value.casefold() == normalized:
            return status_value
    if normalized == "triage":
        return TicketStatus.TRIAGED
    return None


def _status_to_api(value: TicketStatus) -> str:
    if value == TicketStatus.TRIAGED:
        return "triage"
    return value.value.casefold().replace(" ", "_")


def _generate_recommended_actions(
    ticket: TicketRead,
    refs: list[KBArticleRef],
    duplicates: list[dict[str, Any]],
    recent_tickets: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    payload = {
        "ticket": {
            "id": ticket.id,
            "summary": ticket.intelligence.summary,
            "category": ticket.intelligence.category.value,
            "priority": ticket.intelligence.suggested_priority.value,
            "keywords": ticket.intelligence.keywords[:8],
            "app_name": ticket.app_name,
            "environment": ticket.environment.value,
        },
        "similar_tickets_by_score": duplicates[:6],
        "recent_similar_tickets": recent_tickets[:10],
        "knowledge_refs": [
            {
                "kb_id": ref.kb_id,
                "title": ref.title,
                "source": ref.source,
                "relevance_score": ref.relevance_score,
            }
            for ref in refs[:6]
        ],
    }

    try:
        response = get_chat_model().invoke(
            [
                SystemMessage(
                    content=(
                        "You are an IT operations copilot generating incident recommendations. "
                        "Use only provided evidence. Not every ticket is a bug: feature, infra, "
                        "access, and hardware requests can be valid outcomes. "
                        "Return exactly 1 to 4 concise recommended actions, one per line, "
                        "ordered by priority. No bullets, no numbering, no markdown."
                    )
                ),
                HumanMessage(content=json.dumps(payload, ensure_ascii=True)),
            ]
        )
        raw_content = getattr(response, "content", response)
        text = _coerce_llm_text(raw_content)
        actions = _extract_action_lines(text)
        if actions:
            return actions[0], actions
    except Exception:
        logger.exception("Ticket insight recommendation generation failed")

    fallback = "Review retrieved similar tickets and knowledge references before deciding next workflow step."
    return fallback, [fallback]


def _coerce_llm_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(value).strip()


def _extract_action_lines(text: str, *, max_items: int = 4) -> list[str]:
    if not text:
        return []

    cleaned: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        candidate = line.strip().lstrip("-*0123456789. ").strip()
        if not candidate:
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        cleaned.append(candidate)
        seen.add(key)
        if len(cleaned) >= max_items:
            break

    if cleaned:
        return cleaned
    single = text.strip()
    return [single] if single else []
