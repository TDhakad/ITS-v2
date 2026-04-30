"""Simplified LangGraph ReAct agent for IT helpdesk.

Architecture (3 nodes):
  guardrail → agent ↔ tools

The LLM does all reasoning. Python handles only side effects and safety.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re as _re
from functools import lru_cache
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage,
                                     SystemMessage, ToolMessage)
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except Exception:  # pragma: no cover
    SqliteSaver = None  # type: ignore[assignment]

from app.db import SessionLocal
from app.db import create_ticket as _db_create_ticket
from app.db import search_tickets as _db_search_tickets
from app.guardrails import evaluate_input_safety, redact_sensitive_text
from app.llm import get_chat_model
from app.rag import HybridRAGPipeline, context_from_user
from app.schemas import (AgentResponse, ChatMessage, ChatTurnResult,
                         Environment, MessageRole, Priority, ResolutionData,
                         TicketCategory, TicketCreate, TicketIntelligence,
                         TicketStatus, UserClearance, UserRole)
from app.settings import get_settings
from app.ticket_vector import search_ticket_vectors

logger = logging.getLogger(__name__)


# ── Intent classification ──────────────────────────────────────────────────────
# Fast regex pass on the incoming message to inject a tool-selection hint into
# the system prompt, reducing the model's reasoning overhead.

_ANALYTICS_RE = _re.compile(
    r"\b(how many|count|total|breakdown|break.?down|by status|by priority|by category|"
    r"trend|statistics?|report|open tickets?|closed tickets?|per (day|week|month)|"
    r"tickets? (per|by|in|with|for))",
    _re.IGNORECASE,
)
_KB_RE = _re.compile(
    r"\b(how (do|to|can)|steps to|fix|solve|troubleshoot|configure|install|setup|"
    r"reset password|not working|doesn.t work)",
    _re.IGNORECASE,
)
_STATUS_RE = _re.compile(
    r"\b(is there (a|an) ticket|(my|the) ticket (status|about|for)|"
    r"already (filed|a ticket)|existing ticket|check (my )?ticket)",
    _re.IGNORECASE,
)
_CREATE_RE = _re.compile(
    r"\b(create|file|submit|open|raise) (a |an )?(new )?(ticket|issue|request|bug)",
    _re.IGNORECASE,
)

_INTENT_HINTS: dict[str, str] = {
    "analytics": "\n\n[INTENT: analytics] → call analyze_ticket_data immediately.",
    "create": "\n\n[INTENT: create ticket] → gather summary/category/priority then call create_helpdesk_ticket.",
    "status": "\n\n[INTENT: status check] → call search_existing_tickets immediately.",
    "kb": "\n\n[INTENT: kb lookup] → call search_knowledge_base immediately.",
}


def _classify_intent(message: str) -> str | None:
    if _ANALYTICS_RE.search(message):
        return "analytics"
    if _CREATE_RE.search(message):
        return "create"
    if _STATUS_RE.search(message):
        return "status"
    if _KB_RE.search(message):
        return "kb"
    return None


# ── Per-request context ────────────────────────────────────────────────────────
# Tool side effects (ticket_id, kb_refs) propagate back to the caller because a
# per-request dict is installed before graph.invoke().
_REQUEST_CTX: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "helpdesk_request",
    default=None,
)




# ── Graph state ────────────────────────────────────────────────────────────────


class HelpdeskAgentState(TypedDict, total=False):
    # LangChain messages — accumulated across turns via the checkpointer.
    messages: Annotated[list[BaseMessage], add_messages]
    # Session metadata — injected once per turn, treated as read-only.
    user_id: str
    thread_id: str
    app_name: str | None
    environment: Environment
    user_clearance: UserClearance
    # Set by guardrail_node when a request is blocked.
    is_blocked: bool
    route: str


def _request_ctx() -> dict[str, Any]:
    return _REQUEST_CTX.get() or {}


def _scoped_ticket_user_id(ctx: dict[str, Any]) -> str | None:
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


# ── Tools ──────────────────────────────────────────────────────────────────────


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
        docs = rag.retrieve(query, retrieval_ctx, k=5)
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


@tool
def search_existing_tickets(query: str) -> str:
    """Search existing helpdesk tickets by topic, app name, or description.

    Use this when the user asks whether a ticket already exists for a specific issue,
    wants to check the status of a previous request, or asks about past incidents
    (e.g. "is there a ticket for JFrog access?", "any open ticket about VPN?").
    """
    ctx = _request_ctx()
    try:
        with SessionLocal() as db:
            results = _db_search_tickets(
                db,
                query,
                user_id=_scoped_ticket_user_id(ctx),
                project_id=ctx.get("project_id"),
                limit=15,
            )
        if not results:
            return f"No existing tickets found matching '{query}'."
        lines = [f"Found {len(results)} matching ticket(s):\n"]
        for r in results:
            lines.append(
                f"• Ticket #{r['ticket_id']} [{r['status']} / {r['priority']}] — {r['summary']} "
                f"(filed {r['created_at'][:10] if r['created_at'] else 'unknown'})"
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("Ticket search failed")
        return "Ticket search unavailable."


@tool
def analyze_ticket_data(question: str) -> str:
    """Answer exact ticket analytics questions with read-only SQL-backed tools.

    Use this for ticket counts, totals, breakdowns, trends, and bounded lists
    (for example: "how many VPN tickets?", "break VPN tickets down by status",
    or "show the matching VPN tickets"). Do not infer exact totals from
    search_existing_tickets or vector_search_tickets.
    """
    ctx = _request_ctx()
    try:
        from app.admin_analytics import run_admin_analytics_question

        # Scope analytics to the user's project for non-admin roles to prevent
        # cross-project data leakage.
        user_role = ctx.get("user_role", UserRole.USER)
        scope_project_id = (
            None if user_role == UserRole.ADMIN else ctx.get("project_id")
        )
        result = run_admin_analytics_question(question, project_id=scope_project_id)
        return str(result.get("answer") or "No analytics answer was generated.")
    except Exception:
        logger.exception("Ticket analytics failed")
        return "Ticket analytics unavailable."


@tool
def vector_search_tickets(
    query: str,
    status: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
) -> str:
    """Semantic vector search over historical tickets in Pinecone.

    Use this when the user describes an issue in natural language and similar
    past tickets may contain useful context, fixes, duplicate incidents, or
    escalation patterns. This searches ticket summaries, keywords, linked KB
    refs, suggested fixes, and conversation text.

    Optional filters:
    - status: Open, Triaged, In Progress, Resolved, or Closed
    - priority: Low, Medium, High, or Critical
    - tags: comma-separated impact-area slugs, e.g. "access,infra" or "security"
    """
    ctx = _request_ctx()
    # Coerce tags from a comma-separated string to a list (LLMs often pass a string).
    tag_list: list[str] | None = (
        [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    )
    try:
        results = search_ticket_vectors(
            query,
            user_id=_scoped_ticket_user_id(ctx),
            project_id=ctx.get("project_id"),
            project_ids=ctx.get("project_ids"),
            tag_slugs=tag_list,
            status=_enum_value(status, TicketStatus),
            priority=_enum_value(priority, Priority),
            limit=15,
        )
        if not results:
            return f"No semantically similar tickets found for '{query}'."

        lines = [f"Found {len(results)} semantically similar ticket(s):"]
        for index, result in enumerate(results, start=1):
            metadata = result.metadata
            created_at = str(metadata.get("created_at") or "")
            filed = created_at[:10] if created_at else "unknown"
            content = _shorten_tool_text(result.content, max_chars=700)
            lines.append(
                "\n".join(
                    [
                        (
                            f"[{index}] Ticket #{result.ticket_id} "
                            f"[{metadata.get('status', 'unknown')} / "
                            f"{metadata.get('priority', 'unknown')}] "
                            f"score={result.score:.2f}"
                        ),
                        f"summary: {result.summary}",
                        f"category: {metadata.get('category', 'unknown')}; filed: {filed}",
                        f"context: {content}",
                    ]
                )
            )
        return "\n\n".join(lines)
    except Exception:
        logger.exception("Ticket vector search failed")
        return "Ticket vector search unavailable."


@tool
def create_helpdesk_ticket(
    summary: str,
    category: Literal["Bug", "Feature", "UI", "Infra", "Hardware"],
    priority: Literal["Low", "Medium", "High", "Critical"],
    keywords: list[str],
    tags: list[str] | None = None,
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
        conversation: list[ChatMessage] = [
            ChatMessage(
                role=MessageRole.USER if m.type == "human" else MessageRole.ASSISTANT,
                content=str(m.content),
            )
            for m in ctx.get("messages_snapshot", [])
            if hasattr(m, "content") and m.content and m.type in ("human", "ai")
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
                    tag_slugs=tags or [],
                    intelligence=TicketIntelligence(
                        category=TicketCategory(category),
                        suggested_priority=Priority(priority),
                        summary=summary,
                        keywords=keywords[:12],
                        confidence=0.85,
                    ),
                    resolution=ResolutionData(),
                    conversation=conversation,
                ),
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
        data:        List of row objects from the analytics result,
                     e.g. [{"bucket": "High", "count": 13}, ...]
        colors:      Optional list of hex color strings for each series in data_keys order,
                     e.g. ["#6366f1", "#22c55e"]. Choose colors that are visually distinct
                     and match the semantic meaning of the data (red for critical/errors,
                     amber for warnings, green for resolved/good, indigo for neutral).
    """
    ctx = _request_ctx()
    rows = [r for r in data if isinstance(r, dict)]
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


_TOOLS: list[BaseTool] = [
    search_knowledge_base,
    analyze_ticket_data,
    search_existing_tickets,
    vector_search_tickets,
    create_helpdesk_ticket,
    render_chart,
]
_TOOL_MAP: dict[str, BaseTool] = {t.name: t for t in _TOOLS}

_SYSTEM_PROMPT = """\
You are an IT helpdesk assistant. Help users resolve IT issues concisely and professionally.

Tool guidance:
- search_knowledge_base: user has an IT problem → search KB first; return actionable steps.
- analyze_ticket_data: user asks for counts, totals, breakdowns, or trends. Never estimate — always use this tool.
- search_existing_tickets: user asks if a ticket exists or wants a status update.
- vector_search_tickets: user describes symptoms in natural language to find similar past incidents.
- create_helpdesk_ticket: issue needs human intervention or the user explicitly asks. Include impact-area tags: ui, hardware, access, infra, security, network, performance, data.
- render_chart: call this AFTER analyze_ticket_data when the result has multi-row numeric data worth visualising (breakdowns, trends). Do NOT call for single-number answers.

Chart workflow:
1. Call analyze_ticket_data to get the answer.
2. If the result contains a list of rows with counts or numeric values (e.g. by priority, status, date), call render_chart immediately after.
   - Use "bar" for category breakdowns, "line" for time-series.
   - Pass the exact rows from the analytics result as the data list.
   - Always pass a colors list matching the semantic meaning of the data:
     e.g. critical→"#ef4444", high→"#f59e0b", medium→"#6366f1", low→"#22c55e", resolved→"#10b981"
3. After calling render_chart, respond with ONLY a single sentence confirming what was visualised. Do NOT re-list the numbers or write a paragraph. Never draw ASCII charts or text bars.

Ask follow-up questions when you need the app name, error message, or affected scope.
When referencing existing tickets in responses, format them as markdown links: [#123](/tickets/123).
If the KB has no answer, check similar past tickets before creating a new one."""


# ── Graph nodes ────────────────────────────────────────────────────────────────


def guardrail_node(state: HelpdeskAgentState) -> dict[str, Any]:
    last_human = next(
        (m for m in reversed(state.get("messages", [])) if isinstance(m, HumanMessage)),
        None,
    )
    if last_human is None:
        return {}

    decision = evaluate_input_safety(last_human.content)
    if decision.is_safe:
        return {"is_blocked": False}

    # Security event — auto-create a security ticket and block the request.
    ctx = _request_ctx()
    ticket_note = ""
    try:
        with SessionLocal() as db:
            ticket = _db_create_ticket(
                db,
                TicketCreate(
                    user_id=ctx.get("user_id", "anonymous"),
                    thread_id=ctx.get("thread_id", "unknown"),
                    app_name=ctx.get("app_name"),
                    environment=ctx.get("environment", Environment.UNKNOWN),
                    user_clearance=ctx.get("user_clearance", UserClearance.PUBLIC),
                    intelligence=TicketIntelligence(
                        category=TicketCategory.INFRA,
                        suggested_priority=Priority.CRITICAL,
                        summary=f"Security alert: {decision.reason}",
                        keywords=decision.detected_patterns[:6],
                        confidence=0.95,
                    ),
                    resolution=ResolutionData(),
                    raw_context={"guardrail": decision.model_dump(mode="json")},
                ),
            )
        ctx["ticket_id"] = ticket.id
        ticket_note = f" Security ticket #{ticket.id} has been filed for review."
    except Exception:
        logger.exception("Failed to create security guardrail ticket")

    blocked_msg = AIMessage(
        content=(
            "I can't process that request — it appears to involve a security-sensitive "
            f"operation that requires explicit authorization.{ticket_note}"
        )
    )
    return {
        "messages": [blocked_msg],
        "is_blocked": True,
        "route": "blocked",
    }


def agent_node(state: HelpdeskAgentState) -> dict[str, Any]:
    # Snapshot messages so create_helpdesk_ticket can include the conversation.
    ctx = _request_ctx()
    ctx["messages_snapshot"] = list(state.get("messages", []))

    # Inject a one-line intent hint so the LLM skips tool-selection reasoning
    # for clear-cut requests, saving at least one reasoning step.
    last_human = next(
        (m for m in reversed(state.get("messages", [])) if isinstance(m, HumanMessage)),
        None,
    )
    intent = _classify_intent(last_human.content if last_human else "")

    # Build a user-context line so the agent knows who it is talking to.
    user_ctx = f"User: {ctx.get('display_name', 'User')} (role: {ctx.get('user_role', 'user')})"
    if ctx.get("project_id") is not None:
        user_ctx += f", project ID: {ctx['project_id']}"
    system_content = (
        _SYSTEM_PROMPT
        + f"\n\nCurrent session — {user_ctx}."
        + (_INTENT_HINTS.get(intent or "", ""))
    )

    llm = get_chat_model().bind_tools(_TOOLS)
    messages: list[BaseMessage] = [
        SystemMessage(content=system_content),
        *state.get("messages", []),
    ]
    response = llm.invoke(messages)
    return {"messages": [response]}


def tools_node(state: HelpdeskAgentState) -> dict[str, Any]:
    """Execute all tool calls from the last AIMessage and return ToolMessages."""
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}

    results: list[ToolMessage] = []
    for call in last.tool_calls:
        tool_name = call["name"]
        tool_args = call["args"]
        tool_id = call["id"]

        selected = _TOOL_MAP.get(tool_name)
        if selected is None:
            result_content = f"Unknown tool: {tool_name}"
        else:
            try:
                result_content = selected.invoke(tool_args)
            except Exception:
                logger.exception("Tool invocation failed: %s", tool_name)
                result_content = "Tool error."

        results.append(
            ToolMessage(
                content=str(result_content), tool_call_id=tool_id, name=tool_name
            )
        )

    return {"messages": results}


# ── Routing ────────────────────────────────────────────────────────────────────


def _route_after_guardrail(state: HelpdeskAgentState) -> Literal["agent", "__end__"]:
    if state.get("is_blocked"):
        return "__end__"
    return "agent"


def _route_after_agent(state: HelpdeskAgentState) -> Literal["tools", "__end__"]:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "__end__"


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_helpdesk_graph(checkpointer: Any | None = None):
    workflow = StateGraph(HelpdeskAgentState)

    workflow.add_node("guardrail", guardrail_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tools_node)

    workflow.set_entry_point("guardrail")
    workflow.add_conditional_edges(
        "guardrail",
        _route_after_guardrail,
        {"agent": "agent", "__end__": END},
    )
    workflow.add_conditional_edges(
        "agent",
        _route_after_agent,
        {"tools": "tools", "__end__": END},
    )
    workflow.add_edge("tools", "agent")

    return workflow.compile(checkpointer=checkpointer)


# ── Checkpointer (singleton) ───────────────────────────────────────────────────

_CHECKPOINT_CONTEXT: Any | None = None
_CHECKPOINTER: Any | None = None


def sqlite_checkpointer() -> Any | None:
    global _CHECKPOINT_CONTEXT, _CHECKPOINTER
    if SqliteSaver is None:
        return None
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
    settings = get_settings()
    settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = SqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path))
    if hasattr(checkpoint, "__enter__"):
        _CHECKPOINT_CONTEXT = checkpoint
        _CHECKPOINTER = checkpoint.__enter__()
    else:
        _CHECKPOINTER = checkpoint
    return _CHECKPOINTER


@lru_cache
def get_helpdesk_graph():
    return build_helpdesk_graph(checkpointer=sqlite_checkpointer())


def _chat_invoke_config(
    *,
    thread_id: str,
    user_role: str,
    clearance: UserClearance,
    project_id: int | None,
) -> dict[str, Any]:
    tags = ["assistant", "langgraph", "chat-turn"]
    if project_id is not None:
        tags.append("project-scoped")

    metadata: dict[str, Any] = {
        "component": "helpdesk_assistant",
        "thread_id": thread_id,
        "user_role": user_role,
        "clearance": clearance.value,
        "project_scoped": project_id is not None,
    }
    if project_id is not None:
        metadata["project_id"] = project_id

    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 15,
        "run_name": "helpdesk_chat_turn",
        "tags": tags,
        "metadata": metadata,
    }


# ── Public entry point ─────────────────────────────────────────────────────────


def run_chat_turn(
    *,
    message: str,
    thread_id: str,
    user_id: str = "anonymous",
    app_name: str | None = None,
    environment: Environment = Environment.UNKNOWN,
    clearance: UserClearance = UserClearance.PUBLIC,
    project_id: int | None = None,
    project_ids: list[int] | None = None,
    display_name: str = "User",
    user_role: str = UserRole.USER.value,
) -> ChatTurnResult:
    resolved_user_role = str(user_role)

    # Set per-request context — mutable dict shared with tools.
    ctx: dict[str, Any] = {
        "user_id": user_id,
        "thread_id": thread_id,
        "app_name": app_name,
        "environment": environment,
        "user_clearance": clearance,
        "project_id": project_id,
        "project_ids": project_ids,
        "display_name": display_name,
        "user_role": resolved_user_role,
        "kb_refs": [],
        "ticket_id": None,
        "messages_snapshot": [],
    }
    _REQUEST_CTX.set(ctx)

    graph = get_helpdesk_graph()
    final_state = graph.invoke(
        {
            "messages": [HumanMessage(content=message)],
            "user_id": user_id,
            "thread_id": thread_id,
            "app_name": app_name,
            "environment": environment,
            "user_clearance": clearance,
        },
        config=_chat_invoke_config(
            thread_id=thread_id,
            user_role=resolved_user_role,
            clearance=clearance,
            project_id=project_id,
        ),
    )

    # Find the last non-tool-call AI message — that's what the user sees.
    all_messages: list[BaseMessage] = final_state.get("messages", [])
    last_ai = next(
        (
            m
            for m in reversed(all_messages)
            if isinstance(m, AIMessage) and not m.tool_calls
        ),
        None,
    )
    response_text = (
        str(last_ai.content)
        if last_ai
        else "I'm unable to process your request right now."
    )

    # Post-process: scrub any PII / secrets that slipped through.
    redacted, findings = redact_sensitive_text(response_text)
    if findings:
        response_text = redacted

    # Determine the route label used by the API and frontend.
    route: Literal["follow_up", "self_resolution", "ticket_created", "blocked"] = (
        "follow_up"
    )
    if final_state.get("is_blocked") or final_state.get("route") == "blocked":
        route = "blocked"
    elif ctx.get("ticket_id"):
        route = "ticket_created"
    elif ctx.get("kb_refs"):
        route = "self_resolution"

    # If the LLM called render_chart, wrap the response with the chart config.
    agent_response: AgentResponse | None = None
    chart_data = ctx.get("chart")
    if chart_data:
        try:
            from app.schemas import ChartConfiguration
            agent_response = AgentResponse(
                markdown_text=response_text,
                chart=ChartConfiguration(**chart_data),
            )
        except Exception:
            logger.warning("Failed to build AgentResponse from chart ctx", exc_info=True)

    return ChatTurnResult(
        thread_id=thread_id,
        response=response_text,
        route=route,
        ticket_id=ctx.get("ticket_id"),
        linked_kb_articles=ctx.get("kb_refs", []),
        agent_response=agent_response,
    )
