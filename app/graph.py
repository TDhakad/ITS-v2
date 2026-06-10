"""Simplified LangGraph ReAct agent for IT helpdesk.

Architecture (3 nodes):
  guardrail → agent ↔ tools

The LLM does all reasoning. Python handles only side effects and safety.
"""

from __future__ import annotations

import logging
import re as _re
from functools import lru_cache
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except Exception:  # pragma: no cover
    SqliteSaver = None  # type: ignore[assignment]

try:
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool
except Exception:
    PostgresSaver = None
    ConnectionPool = None

from app.db import SessionLocal
from app.db import create_ticket as _db_create_ticket
from app.db import enqueue_background_job
from app.guardrails import evaluate_input_safety, redact_sensitive_text
from app.llm import get_chat_model
from app.schemas import (
    AgentResponse,
    ChatMessage,
    ChatTurnResult,
    Environment,
    Priority,
    ResolutionData,
    TicketCategory,
    TicketCreate,
    TicketIntelligence,
    UserClearance,
    UserRole,
)
from app.settings import get_settings
from app.tools import TOOL_MAP as _TOOL_MAP
from app.tools import TOOLS as _TOOLS
from app.tools import install_request_context
from app.tools import request_context

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
    r"already (filed|a ticket)|existing ticket|check (my )?ticket|"
    r"(find|search|show|list|get|check)\b.*\b(status|ticket|issue)|"
    r"\bstatus\b.*\b(ticket|issue|component|app|service))",
    _re.IGNORECASE,
)
_CREATE_RE = _re.compile(
    r"\b(create|file|submit|open|raise) (a |an )?(new )?(ticket|issue|request|bug)",
    _re.IGNORECASE,
)

_INTENT_HINTS: dict[str, str] = {
    "analytics": "\n\n[INTENT: analytics] → call analyze_ticket_data immediately.",
    "create": (
        "\n\n[INTENT: create ticket] → FIRST check for duplicates by calling find_tickets (semantic=true) "
        "with the requested summary. If similar tickets exist, present them and ask the user for confirmation "
        "before calling create_helpdesk_ticket."
    ),
    "status": "\n\n[INTENT: status check] → call find_tickets, then retrieve_ticket_comments if the user wants an update or progress details.",
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
    # Set by tools_node when a tool already returned a user-facing answer.
    direct_response: str | None
    route: str
    # Counts completed tool rounds to prevent infinite tool-call loops.
    tool_rounds: int


_MAX_TOOL_ROUNDS = 4

_SYSTEM_PROMPT = """\
You are an IT helpdesk assistant. Help users resolve IT issues concisely and professionally.

Use more tools only if result from previous tool calls are insufficient or doesn't cover complete picture.

### CORE BEHAVIORS & GUARDRAILS
- Think step-by-step. Identify the user's core intent before selecting a tool.
- NEVER invent or guess ticket IDs, metric numbers, or root causes.
- NEVER retry a tool with the exact same parameters if it returns "no results" or an empty list. Accept that as final result.
- ALWAYS format ticket references as markdown links: [#123](/tickets/123).
- BEFORE creating a new ticket (even if explicitly requested), you MUST call find_tickets (semantic=true) with the ticket description/summary to check for duplicates. If you find highly similar or duplicate tickets, present them to the user and ask for confirmation before proceeding to file a new one.
- Ask follow-up questions when you need the app name, error message, or affected scope etc.

Tool choice:
IF the user asks "How to...", "What are the steps to...", or needs troubleshooting setup:
THEN USE: `search_knowledge_base`

IF the user asks to find an existing issue, check for duplicates, or get a high-level status by component/title:
THEN USE: `find_tickets` (Set `include_comments=true` if they ask for progress updates).

IF the user asks for exact counts, totals, breakdowns, trends, or reporting (e.g., "How many P1s today?"):
THEN USE: `analyze_ticket_data`

IF the user asks for Root Cause Analysis (RCA), blockers, dependencies, or why something happened:
THEN USE: `retrieve_ticket_comments` (Filter by ticket_ids if already known).

IF you have successfully run `analyze_ticket_data` AND the result contains multiple data points that would benefit from visual comparison (e.g., trends over time, or multiple categories/groupings):
THEN proactively USE: `render_chart` to enhance your response, even if the user did not explicitly ask for a visual.
- Use 'bar' for comparing categories.
- Use 'line' for time series and trends.
- NEVER trigger a chart for a single static number or a flat list of text.
- Structure the chart so a viewer immediately understands the comparison. Choose how many series
  and what x_axis represents based on what makes the data clearest, not what is easiest to query.
- Assign colors that reflect the meaning of each series. A chart where every bar or line is the
  same color is harder to read — use distinct, meaningful colors whenever there are multiple series.

IF the user asks to file a new issue, or human intervention is definitively required:
THEN: First check for duplicates using find_tickets (semantic=true). If duplicates are found, show them and ask if the user wants to update/view the existing ticket. If the user confirms a new ticket is still needed, THEN USE: `create_helpdesk_ticket` (Tag accurately: ui, hardware, access, infra, security, network, performance, data).

### TOOL CHAINING
You are encouraged to chain tools when necessary. For example, use `find_tickets` to get a Ticket ID, then immediately use `retrieve_ticket_comments` to read the engineer discussion for that specific ID before responding to the user.
"""


_DIRECT_RESPONSE_TOOLS: frozenset[str] = frozenset({"create_helpdesk_ticket"})


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
        return {"is_blocked": False, "direct_response": None}

    # Security event — auto-create a security ticket and block the request.
    ctx = request_context()
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
    ctx = request_context()
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
        return {"direct_response": None}

    results: list[ToolMessage] = []
    direct_chunks: list[str] = []
    response_ready = True
    for call in last.tool_calls:
        tool_name = call["name"]
        tool_args = call["args"]
        tool_id = call["id"]

        selected = _TOOL_MAP.get(tool_name)
        if selected is None:
            result_content = f"Unknown tool: {tool_name}"
            response_ready = False
        else:
            try:
                result_content = selected.invoke(tool_args)
            except Exception:
                logger.exception("Tool invocation failed: %s", tool_name)
                result_content = "Tool error."
                response_ready = False

        if tool_name in _DIRECT_RESPONSE_TOOLS:
            direct_chunks.append(str(result_content).strip())
        else:
            response_ready = False

        results.append(
            ToolMessage(
                content=str(result_content), tool_call_id=tool_id, name=tool_name
            )
        )

    tool_rounds = int(state.get("tool_rounds") or 0) + 1

    if response_ready and direct_chunks:
        direct_response = "\n\n".join(chunk for chunk in direct_chunks if chunk)
        return {
            "messages": [*results, AIMessage(content=direct_response)],
            "direct_response": direct_response,
            "tool_rounds": tool_rounds,
        }

    return {"messages": results, "direct_response": None, "tool_rounds": tool_rounds}


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


def _route_after_tools(state: HelpdeskAgentState) -> Literal["agent", "__end__"]:
    if state.get("direct_response"):
        return "__end__"
    if int(state.get("tool_rounds") or 0) >= _MAX_TOOL_ROUNDS:
        return "__end__"
    return "agent"


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
    workflow.add_conditional_edges(
        "tools",
        _route_after_tools,
        {"agent": "agent", "__end__": END},
    )

    return workflow.compile(checkpointer=checkpointer)


# ── Checkpointer (singleton) ───────────────────────────────────────────────────

_CHECKPOINT_CONTEXT: Any | None = None
_CHECKPOINTER: Any | None = None


def get_checkpointer() -> Any | None:
    global _CHECKPOINT_CONTEXT, _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
        
    settings = get_settings()
    db_url = settings.database_url
    
    if db_url.startswith("postgresql") or db_url.startswith("postgres"):
        if PostgresSaver is None or ConnectionPool is None:
            logger.warning("PostgresSaver dependencies missing. Checkpointer unavailable.")
            return None
            
        clean_url = db_url.replace("+psycopg2", "").replace("+psycopg", "")
        pool = ConnectionPool(conninfo=clean_url, max_size=10, kwargs={"autocommit": True})
        checkpoint = PostgresSaver(pool)
        checkpoint.setup()
        
        _CHECKPOINT_CONTEXT = pool
        _CHECKPOINTER = checkpoint
        return _CHECKPOINTER
    else:
        if SqliteSaver is None:
            return None
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
    return build_helpdesk_graph(checkpointer=get_checkpointer())


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
        "recursion_limit": 14,
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
    install_request_context(ctx)

    graph = get_helpdesk_graph()
    final_state = graph.invoke(
        {
            "messages": [HumanMessage(content=message)],
            "user_id": user_id,
            "thread_id": thread_id,
            "app_name": app_name,
            "environment": environment,
            # Reset per-turn tool counter so checkpointer never carries a
            # stale value from the previous turn into this one.
            "tool_rounds": 0,
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
            logger.warning(
                "Failed to build AgentResponse from chart ctx", exc_info=True
            )

    return ChatTurnResult(
        thread_id=thread_id,
        response=response_text,
        route=route,
        ticket_id=ctx.get("ticket_id"),
        linked_kb_articles=ctx.get("kb_refs", []),
        agent_response=agent_response,
    )
