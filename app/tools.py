"""LangGraph tool implementations for the IT helpdesk assistant."""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from app.db import SessionLocal
from app.db import create_ticket as _db_create_ticket
from app.db import enqueue_background_job, get_kb_project_ids
from app.db import list_ticket_comments as _db_list_ticket_comments
from app.db import search_tickets as _db_search_tickets
from app.comment_vector import CommentVectorSearchUnavailable, search_comment_vectors
from app.rag import HybridRAGPipeline, context_from_user
from app.schemas import (
    ChatMessage,
    CommentMetadataFilters,
    Environment,
    MessageRole,
    Priority,
    ResolutionData,
    TicketCategory,
    TicketCommentRetrieverInput,
    TicketCreate,
    TicketIntelligence,
    TicketStatus,
    UserClearance,
    UserRole,
)

logger = logging.getLogger(__name__)

_REQUEST_CTX: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "helpdesk_request",
    default=None,
)


def install_request_context(ctx: dict[str, Any]) -> None:
    _REQUEST_CTX.set(ctx)


def request_context() -> dict[str, Any]:
    return _REQUEST_CTX.get() or {}


def _request_ctx() -> dict[str, Any]:
    return request_context()


def _scoped_ticket_user_id(ctx: dict[str, Any]) -> str | None:
    if ctx.get("project_id") is not None or ctx.get("project_ids"):
        return None
    if ctx.get("user_role") in {UserRole.ADMIN.value, UserRole.AGENT.value}:
        return None
    try:
        clearance = UserClearance(ctx.get("user_clearance", UserClearance.PUBLIC))
    except ValueError:
        clearance = UserClearance.PUBLIC
    if clearance == UserClearance.RESTRICTED:
        return None
    return ctx.get("user_id")


def _enum_value(value: str | None, enum_cls: Any) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().casefold().replace("-", " ").replace("_", " ")
    for item in enum_cls:
        item_value = str(item.value)
        item_normalized = item_value.casefold().replace("-", " ").replace("_", " ")
        if normalized in {item_normalized, item.name.casefold()}:
            return item_value
    return str(value).strip()


def _shorten_tool_text(value: str, *, max_chars: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_chars:
        return clean
    return f"{clean[:max_chars].rstrip()}..."


@tool
def search_knowledge_base(query: str) -> str:
    """Search the IT helpdesk knowledge base for articles, runbooks, and how-to guides.

    Use this when a user has an IT issue that might be covered by existing documentation
    (password resets, VPN problems, software installs, common errors, etc.).
    Always try this before creating a ticket for common issues.
    """
    ctx = _request_ctx()
    try:
        rag = HybridRAGPipeline()
        retrieval_ctx = context_from_user(
            category=None,
            app_name=ctx.get("app_name"),
            environment=ctx.get("environment", Environment.UNKNOWN),
            clearance=ctx.get("user_clearance", UserClearance.PUBLIC),
        )
        docs = _filter_kb_docs_for_context(rag.retrieve(query, retrieval_ctx, k=5), ctx)
        if not docs:
            return "No relevant knowledge base articles found for this query."
        refs = rag.article_refs(docs)
        ctx["kb_refs"] = refs
        blocks: list[str] = []
        for i, doc in enumerate(docs, 1):
            title = doc.metadata.get("title", "Article")
            blocks.append(f"[{i}] **{title}**\n{doc.page_content[:900]}")
        return "\n\n---\n\n".join(blocks)
    except Exception:
        logger.exception("Knowledge base search failed")
        return "Knowledge base search unavailable."


def _format_ticket_search_results(
    *,
    query: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    include_comments: bool = False,
    limit: int = 8,
    semantic: bool = False,
) -> str:
    ctx = _request_ctx()
    tag_list = _metadata_list(tags)
    selected_limit = max(1, min(int(limit), 15))
    try:
        with SessionLocal() as db:
            results = _db_search_tickets(
                db,
                query,
                user_id=_scoped_ticket_user_id(ctx),
                project_id=ctx.get("project_id"),
                tag_slugs=tag_list or None,
                status=_enum_value(status, TicketStatus),
                priority=_enum_value(priority, Priority),
                limit=selected_limit,
                use_vector=semantic,
            )
            if not results:
                return f"No existing tickets found{f' matching {query!r}' if query else ''}."
            terms = _ticket_query_terms(query) if query else []
            if len(terms) > 1:
                strong_results = [
                    result
                    for result in results
                    if int(result.get("keyword_score") or 0) >= len(terms)
                ]
                if strong_results:
                    results = strong_results
            lines = [f"Found {len(results)} matching ticket(s):\n"]
            for result in results:
                ticket_id = int(result["ticket_id"])
                app_name = str(result.get("app_name") or "").strip()
                app_text = (
                    f"; app: {app_name}"
                    if app_name and app_name.casefold() != "general"
                    else ""
                )
                lines.append(
                    f"- [#{ticket_id}](/tickets/{ticket_id}) — {result['summary']} "
                    f"(status: {result['status']}; priority: {result['priority']}"
                    f"{app_text}; "
                    f"filed {result['created_at'][:10] if result['created_at'] else 'unknown'})"
                )

                if not include_comments:
                    continue

                comments = _db_list_ticket_comments(db, ticket_id)
                if not comments:
                    lines.append("  Recent comments: none")
                    continue

                lines.append("  Recent comments:")
                for comment in comments[-3:]:
                    author = getattr(comment, "author", None)
                    author_display_name = (
                        getattr(author, "display_name", None)
                        or getattr(author, "email", None)
                        or f"User #{comment.author_user_id}"
                    )
                    comment_text = _shorten_tool_text(
                        str(comment.content), max_chars=220
                    )
                    lines.append(
                        f"  - C-{comment.id} by {author_display_name}: {comment_text}"
                    )
        return "\n".join(lines)
    except Exception:
        logger.exception("Ticket search failed")
        return "Ticket search unavailable."


@tool
def find_tickets(
    query: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    include_comments: bool = False,
    limit: int = 8,
    semantic: bool = False,
) -> str:
    """Find existing tickets by title, topic, component, app, or symptoms.

    Use for ticket lookups, duplicate checks, and status questions such as
    "find CSV component status". Pass only the important search phrase in query
    (for example "CSV component"). Set include_comments only when the user asks
    for progress details, blockers, or discussion history. Set semantic=true only
    when the user describes symptoms or asks for similar historical incidents.

    To list the most recent or latest tickets, omit query (or set it to null)
    and set limit to the desired count.
    """

    return _format_ticket_search_results(
        query=query,
        status=status,
        priority=priority,
        tags=tags,
        include_comments=include_comments,
        limit=limit,
        semantic=semantic,
    )


@tool
def analyze_ticket_data(question: str) -> str:
    """Answer exact ticket analytics questions with read-only SQL-backed data.

    Use this for ticket counts, totals, breakdowns, trends, and bounded lists
    (for example: "how many VPN tickets?", "break VPN tickets down by status",
    or "show the matching VPN tickets"). Do not use this for a simple status
    lookup by title/component; use find_tickets instead.
    """
    ctx = _request_ctx()
    # Deduplicate: return the cached result if the same question was already
    # answered during this request. Prevents the LLM from driving repeated
    # SQL round-trips when it retries the same analytics call.
    cache_key = f"analytics:{question.strip().casefold()}"
    if cache_key in ctx:
        logger.debug("analyze_ticket_data cache hit for: %s", question[:120])
        return ctx[cache_key]
    try:
        from app.admin_analytics import run_admin_analytics_question
        from app.schemas import UserRole

        # Scope analytics to the user's project for non-admin roles to prevent
        # cross-project data leakage.
        user_role = ctx.get("user_role", UserRole.USER)
        scope_project_id = (
            None if user_role == UserRole.ADMIN else ctx.get("project_id")
        )
        result = run_admin_analytics_question(question, project_id=scope_project_id)
        answer = str(result.get("answer") or "No analytics answer was generated.")
        ctx[cache_key] = answer
        return answer
    except Exception:
        logger.exception("Ticket analytics failed")
        return "Ticket analytics unavailable."


@tool(args_schema=TicketCommentRetrieverInput)
def retrieve_ticket_comments(
    query: str,
    metadata_filters: CommentMetadataFilters | None = None,
    limit: int = 8,
) -> str:
    """Semantic vector retrieval over ticket comments with strict metadata filtering.

    Vector Search (Comments)
    - PURPOSE: reveals the "why" and "how" from chronological engineer discussion.
    - USE THIS FOR:
      * Root Cause Analysis (RCA)
      * Blockers and dependencies
      * Historical context from similar incidents
      * Qualitative status updates from engineers

    metadata_filters must use ONLY these keys:
    - ticket_ids
    - comment_ids
    - tags
    - references_tickets
    - references_systems
    - references_people
    - author_user_ids
    - parent_comment_ids
    - min_signal_strength
    - max_signal_strength
    """
    ctx = _request_ctx()
    selected_limit = max(1, min(int(limit), 20))
    try:
        results = search_comment_vectors(
            query,
            metadata_filters=metadata_filters,
            project_id=ctx.get("project_id"),
            project_ids=ctx.get("project_ids"),
            user_clearance=str(ctx.get("user_clearance", UserClearance.PUBLIC)),
            limit=selected_limit,
        )
        if not results:
            return (
                "No relevant ticket comments found for that query and metadata filters."
            )

        lines = [f"Found {len(results)} relevant ticket comment(s):"]
        for index, result in enumerate(results, start=1):
            metadata = result.metadata
            tags = ", ".join(_metadata_list(metadata.get("tags"))) or "none"
            references_tickets = (
                ", ".join(_metadata_list(metadata.get("references_tickets"))) or "none"
            )
            references_systems = (
                ", ".join(_metadata_list(metadata.get("references_systems"))) or "none"
            )
            references_people = (
                ", ".join(_metadata_list(metadata.get("references_people"))) or "none"
            )
            lines.append(
                "\n".join(
                    [
                        (
                            f"[{index}] Ticket #{result.ticket_id}, Comment C-{result.comment_id} "
                            f"score={result.score:.2f}"
                        ),
                        f"summary: {_shorten_tool_text(result.summary, max_chars=200) or 'n/a'}",
                        f"tags: {tags}",
                        f"refs tickets: {references_tickets}",
                        f"refs systems: {references_systems}",
                        f"refs people: {references_people}",
                        f"signal_strength: {float(metadata.get('signal_strength') or 0.0):.2f}",
                        f"context: {_shorten_tool_text(result.content, max_chars=600)}",
                    ]
                )
            )
        return "\n\n".join(lines)
    except CommentVectorSearchUnavailable:
        return "Ticket comment vector search unavailable."
    except Exception:
        logger.exception("Ticket comment retriever failed")
        return "Ticket comment retriever unavailable."


@tool
def create_helpdesk_ticket(
    summary: str,
    category: Literal["Bug", "Feature", "UI", "Infra", "Hardware"],
    priority: Literal["Low", "Medium", "High", "Critical"],
    keywords: list[str] | str,
    tags: list[str] | str | None = None,
) -> str:
    """Create a helpdesk ticket when the issue requires human intervention.

    Use this for: access requests, hardware replacements, privilege escalations,
    problems that cannot be solved from documentation, or when the user explicitly
    asks to file a ticket.

    tags: optional list of impact-area slugs — pick from:
      ui, hardware, access, infra, security, network, performance, data
    """
    ctx = _request_ctx()
    try:
        normalized_keywords = _metadata_list(keywords)[:12]
        normalized_tags = _normalize_ticket_tags(tags)
        conversation: list[ChatMessage] = [
            ChatMessage(
                role=(
                    MessageRole.USER
                    if message.type == "human"
                    else MessageRole.ASSISTANT
                ),
                content=str(message.content),
            )
            for message in ctx.get("messages_snapshot", [])
            if hasattr(message, "content")
            and message.content
            and message.type in ("human", "ai")
        ]
        with SessionLocal() as db:
            ticket = _db_create_ticket(
                db,
                TicketCreate(
                    user_id=ctx.get("user_id", "anonymous"),
                    thread_id=ctx.get("thread_id", "unknown"),
                    app_name=ctx.get("app_name"),
                    environment=ctx.get("environment", Environment.UNKNOWN),
                    user_clearance=ctx.get("user_clearance", UserClearance.PUBLIC),
                    project_id=ctx.get("project_id"),
                    tag_slugs=normalized_tags,
                    intelligence=TicketIntelligence(
                        category=TicketCategory(category),
                        suggested_priority=Priority(priority),
                        summary=summary,
                        keywords=normalized_keywords,
                        confidence=0.85,
                    ),
                    resolution=ResolutionData(),
                    conversation=conversation,
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
        ctx["ticket_id"] = ticket.id
        return (
            f"Ticket #{ticket.id} created successfully. "
            f"Category: {category}, Priority: {priority}. "
            "The helpdesk team will review and follow up with you."
        )
    except Exception:
        logger.exception("Helpdesk ticket creation failed")
        return "Failed to create ticket."


@tool
def render_chart(
    chart_type: Literal["line", "bar"],
    title: str,
    x_axis_key: str,
    data_keys: list[str],
    data: list[dict[str, Any]],
    colors: list[str] | None = None,
) -> str:
    """Render an interactive chart in the frontend.

    Call this AFTER analyze_ticket_data when the result contains multi-row
    numeric data that is better understood visually (e.g. breakdowns, trends).
    Do NOT call this for single-number answers.

    Args:
        chart_type:  "bar" for category comparisons, "line" for time-series trends.
        title:       Short descriptive chart title.
        x_axis_key:  The column name to use as the X-axis label.
        data_keys:   List of numeric column names to plot as series on the Y-axis.
                     Structure the data so a viewer can immediately read the comparison without
                     decoding concatenated labels. Each series in data_keys should represent one
                     meaningful dimension of the data, and each row in `data` should carry values
                     for every series.
        data:        List of row objects where each object has the x_axis_key field plus one
                     numeric field per entry in data_keys.
        colors:      Optional list of hex colors, one per series in data_keys order.
                     When provided, choose colors that reflect the meaning of each series so the
                     chart communicates intent at a glance — not just aesthetic variety.
                     Omit only when a single neutral color is genuinely sufficient.
    """
    ctx = _request_ctx()
    rows = [row for row in data if isinstance(row, dict)]
    if len(rows) < 2:
        return "Chart skipped: data must contain at least 2 rows."
    ctx["chart"] = {
        "chart_type": chart_type,
        "title": title,
        "data": rows,
        "x_axis_key": x_axis_key,
        "data_keys": data_keys,
        "colors": colors,
    }
    return f"Chart queued: {chart_type} chart titled '{title}' with {len(rows)} rows."


def _metadata_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        if "," in value:
            return [part.strip() for part in value.split(",") if part.strip()]
        return [value.strip()]
    text = str(value).strip()
    return [text] if text else []


def _ticket_query_terms(value: str) -> list[str]:
    return [
        term.casefold()
        for term in str(value).split()
        if len(term) > 2 and term.casefold() not in {"the", "and", "for", "with"}
    ]


def _normalize_ticket_tags(value: Any) -> list[str]:
    allowed = {
        "ui",
        "hardware",
        "access",
        "infra",
        "security",
        "network",
        "performance",
        "data",
    }
    aliases = {
        "infrastructure": "infra",
        "networking": "network",
        "permissions": "access",
    }

    result: list[str] = []
    seen: set[str] = set()
    for raw in _metadata_list(value):
        normalized = raw.strip().casefold().replace("-", "_").replace(" ", "_")
        normalized = aliases.get(normalized, normalized)
        if normalized in allowed and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _filter_kb_docs_for_context(docs: list[Any], ctx: dict[str, Any]) -> list[Any]:
    project_id = ctx.get("project_id")
    project_ids = ctx.get("project_ids")
    allowed_projects = {
        int(value)
        for value in ([project_id] if project_id is not None else (project_ids or []))
    }
    if not allowed_projects:
        return docs

    filtered: list[Any] = []
    with SessionLocal() as db:
        for doc in docs:
            linked_project_ids: set[int] = set()
            for identifier in _kb_identifiers(getattr(doc, "metadata", {}) or {}):
                linked_project_ids.update(get_kb_project_ids(db, identifier))
            if not linked_project_ids or linked_project_ids & allowed_projects:
                filtered.append(doc)
    return filtered


def _kb_identifiers(metadata: dict[str, Any]) -> list[str]:
    values = [
        metadata.get("kb_id"),
        metadata.get("source_id"),
        metadata.get("source"),
    ]
    seen: set[str] = set()
    identifiers: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for candidate in (text, text.rsplit("/", 1)[-1]):
            normalized = candidate.removesuffix(".md").removesuffix(".markdown")
            if normalized and normalized not in seen:
                seen.add(normalized)
                identifiers.append(normalized)
    return identifiers


TOOLS: list[BaseTool] = [
    search_knowledge_base,
    find_tickets,
    analyze_ticket_data,
    retrieve_ticket_comments,
    create_helpdesk_ticket,
    render_chart,
]
TOOL_MAP: dict[str, BaseTool] = {tool.name: tool for tool in TOOLS}
