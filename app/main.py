from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
from app.db import (
    add_project_member,
    create_project,
    find_duplicate_candidates,
    get_session,
    get_ticket,
    init_db,
    list_projects,
    list_tags,
    list_tickets,
)
from app.db import (
    create_ticket as persist_ticket,
)
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
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"
FRONTEND_ASSETS = FRONTEND_DIST / "assets"
if FRONTEND_ASSETS.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_ASSETS)), name="frontend-assets")
_db_initialized = False
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, max_length=120)
    thread_id: str | None = Field(default=None, max_length=120)
    # user_id / clearance are resolved from session; these are fallbacks for unauthenticated use.
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
    title: str | None = Field(default=None, max_length=300)
    app_name: str | None = Field(default=None, max_length=120)
    environment: Environment = Environment.UNKNOWN
    clearance: UserClearance = UserClearance.PUBLIC
    category: TicketCategory = TicketCategory.INFRA
    priority: Priority = Priority.MEDIUM
    keywords: list[str] = Field(default_factory=list, max_length=12)
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.on_event("startup")
def startup() -> None:
    get_settings().ensure_local_dirs()
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
def chat_page(request: Request) -> Response:
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
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
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
    return templates.TemplateResponse(request, "admin.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/chat")
def chat_turn(
    payload: ChatRequest,
    request: Request,
    db: DBSession,
    current_user: OptionalUser,
) -> dict[str, Any]:
    _ensure_db()
    settings = get_settings()
    # Resolve identity from session first; fallback to request body for unauthenticated use.
    if current_user:
        resolved_user_id = str(current_user.id)
        resolved_clearance = current_user.clearance
    else:
        resolved_user_id = payload.user_id
        resolved_clearance = payload.clearance or UserClearance(settings.standard_user_clearance)
    thread_id = payload.thread_id or payload.conversation_id or f"{resolved_user_id}-default"
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
                project_id=payload.project_id,
            )
    except Exception as exc:
        logger.exception("Chat backend unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat backend unavailable. Check server logs for initialization details.",
        ) from exc

    ticket_id = _result_value(result, "ticket_id")
    ticket = _safe_get_ticket(db, ticket_id) if ticket_id else None
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
    if ticket:
        response["ticket"] = ticket_to_api(ticket)
    return response


@app.post("/api/tickets", status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreateRequest,
    db: DBSession,
) -> dict[str, Any]:
    _ensure_db()
    try:
        summary = payload.title or payload.description.strip().splitlines()[0][:120]
        ticket = persist_ticket(
            db,
            TicketCreate(
                status=TicketStatus.OPEN,
                user_id=payload.user_id,
                thread_id=payload.thread_id,
                app_name=payload.app_name,
                environment=payload.environment,
                user_clearance=payload.clearance,
                intelligence=TicketIntelligence(
                    category=payload.category,
                    suggested_priority=payload.priority,
                    summary=summary,
                    keywords=payload.keywords,
                    confidence=float(payload.metadata.get("confidence", 0.0) or 0.0),
                ),
                resolution=ResolutionData(),
                conversation=[{"role": "user", "content": payload.description}],
                raw_context={"description": payload.description, "metadata": payload.metadata},
            ),
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
    status_filter: str | None = Query(default=None, alias="status"),
    user_id: str | None = None,
    project_id: int | None = Query(default=None),
    tag_slug: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, list[dict[str, Any]]]:
    _ensure_db()
    parsed_status = _parse_status(status_filter)
    try:
        found = list_tickets(
            db,
            parsed_status,
            project_id=project_id,
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
    return {"tickets": [ticket_to_api(ticket) for ticket in found]}


@app.get("/api/tickets/{ticket_id}")
def ticket_detail(ticket_id: int, db: DBSession) -> dict[str, Any]:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket_to_api(ticket, include_conversation=True)


@app.get("/api/tickets/{ticket_id}/insights")
def ticket_insights(ticket_id: int, db: DBSession) -> dict[str, Any]:
    _ensure_db()
    ticket = _safe_get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    duplicates = find_duplicate_candidates(
        db,
        ticket.intelligence.keywords,
        exclude_ticket_id=ticket.id,
        limit=5,
    )
    kb_refs = list(ticket.resolution.linked_kb_articles)
    suggested_fixes = list(ticket.resolution.suggested_fixes)
    retrieval_available = True

    try:
        rag_module = importlib.import_module("app.rag")
        rag_class = rag_module.HybridRAGPipeline
        context_factory = rag_module.context_from_user
        rag = rag_class()
        context = context_factory(
            category=ticket.intelligence.category,
            app_name=ticket.app_name,
            environment=ticket.environment,
            clearance=UserClearance.INTERNAL,
        )
        docs = rag.retrieve(ticket.intelligence.summary, context, k=5)
        kb_refs = rag.article_refs(docs) or kb_refs
    except Exception:
        logger.exception("Ticket insight knowledge retrieval failed")
        retrieval_available = False
        docs = []

    if not suggested_fixes:
        suggested_fixes = _suggest_fixes(ticket, kb_refs)

    citations = [_kb_ref_to_api(ref) for ref in kb_refs]
    return {
        "ticket_id": ticket.id,
        "summary": ticket.intelligence.summary,
        "recommended_action": (
            suggested_fixes[0] if suggested_fixes else "Review the ticket context."
        ),
        "suggested_priority": ticket.intelligence.suggested_priority.value,
        "signals": [
            ticket.intelligence.category.value,
            *ticket.intelligence.keywords[:6],
            *(f"duplicate #{duplicate['ticket_id']}" for duplicate in duplicates[:3]),
        ],
        "citations": citations,
        "references": citations,
        "duplicates": duplicates,
        "suggested_fixes": suggested_fixes,
        "retrieved_chunks": len(docs),
        "retrieval_available": retrieval_available,
    }


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
        ticket_payload = [ticket_to_api(ticket) for ticket in list_tickets(db, None, limit=500)]
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
    user, token = login(db, payload.email, payload.password,
                        user_agent=user_agent, ip_address=ip)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,   # set True behind HTTPS in production
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
    _: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    records = list_projects(db)
    return {"projects": [
        {"id": r.id, "name": r.name, "slug": r.slug,
         "description": r.description, "owner_id": r.owner_id}
        for r in records
    ]}


@app.post("/api/projects", status_code=status.HTTP_201_CREATED)
def post_project(
    payload: ProjectCreateRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> dict[str, Any]:
    _ensure_db()
    record = create_project(db, ProjectCreate(
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        owner_id=current_user.id,
    ))
    add_project_member(db, record.id, current_user.id, ProjectAccessLevel.OWNER.value)
    return {"id": record.id, "name": record.name, "slug": record.slug}


# ── Tag routes ────────────────────────────────────────────────────────────────

@app.get("/api/tags")
def get_tags(db: DBSession) -> dict[str, Any]:
    _ensure_db()
    tags = list_tags(db)
    return {"tags": [{"id": t.id, "name": t.name, "slug": t.slug, "color": t.color}
                     for t in tags]}


@app.get("/{frontend_path:path}", include_in_schema=False)
def frontend_fallback(frontend_path: str) -> FileResponse:
    """Serve React client routes from the production build when available."""
    blocked_prefixes = ("api/", "auth/", "static/", "assets/")
    blocked_exact = {"api", "auth", "static", "assets", "docs", "openapi.json"}
    if frontend_path in blocked_exact or frontend_path.startswith(blocked_prefixes):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not FRONTEND_INDEX.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(FRONTEND_INDEX)


def _result_value(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


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


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def ticket_to_api(ticket: TicketRead, include_conversation: bool = False) -> dict[str, Any]:
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
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "linked_kb_articles": [
            _kb_ref_to_api(ref)
            for ref in ticket.resolution.linked_kb_articles
        ],
        "duplicate_ticket_ids": ticket.resolution.duplicate_ticket_ids,
    }
    if include_conversation:
        payload["conversation"] = [
            message.model_dump(mode="json")
            for message in ticket.conversation
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


def _suggest_fixes(ticket: TicketRead, refs: list[KBArticleRef]) -> list[str]:
    category = ticket.intelligence.category
    if refs:
        return [f"Review {refs[0].title} and compare it with the reported symptoms."]
    if category == TicketCategory.UI:
        return [
            "Reproduce in a supported browser and collect screenshot, console errors, "
            "and cache state."
        ]
    if category == TicketCategory.HARDWARE:
        return [
            "Confirm asset tag, device health, recent physical changes, and replacement "
            "eligibility."
        ]
    if category == TicketCategory.INFRA:
        return [
            "Check service health, access scope, recent changes, and whether elevated "
            "access is required."
        ]
    if category == TicketCategory.BUG:
        return [
            "Reproduce with exact steps, identify last known good version, and attach "
            "logs or traces."
        ]
    return [
        "Clarify business impact, acceptance criteria, and whether this is a new "
        "request or regression."
    ]
