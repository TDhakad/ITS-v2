"""Read-only SQL analytics agent for the admin dashboard."""

from __future__ import annotations

import ast
import json
import logging
import re
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from functools import lru_cache
from time import perf_counter
from typing import Annotated, Any, Literal, TypedDict

from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.agent_toolkits.sql.base import create_sql_agent
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, event

from app.llm import get_chat_model
from app.schemas import Priority, TicketCategory, TicketStatus
from app.settings import get_settings
from app.ticket_vector import search_ticket_vectors

logger = logging.getLogger("uvicorn.error")

READ_ONLY_TABLES = [
    "tickets",
    "ticket_messages",
    "ticket_kb_links",
    "duplicate_ticket_links",
    "ticket_tags",
    "tags",
    "users",
    "projects",
    "project_members",
]

CUSTOM_TABLE_INFO = {
    "users": """CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email VARCHAR(254),
    display_name VARCHAR(120),
    role VARCHAR(40),
    clearance VARCHAR(40),
    is_active BOOLEAN,
    created_at DATETIME,
    last_login_at DATETIME
)

/*
Only these user columns are available for analytics. Password hashes and session
data are intentionally excluded.
*/"""
}

SQL_AGENT_PREFIX = """You are a read-only admin analytics agent for an IT ticketing system.

You are interacting with a {dialect} database. Unless the admin asks for more rows,
limit detail queries to at most {top_k} rows.

Strict rules:
- Only answer analytics and reporting questions about tickets and related operational data.
- Only use read-only SQL: SELECT, WITH, EXPLAIN, and schema inspection PRAGMA queries.
- Never execute INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, REPLACE, MERGE,
  ATTACH, DETACH, VACUUM, REINDEX, or ANALYZE.
- Always use the SQL checker before executing a query.
- If a query has a syntax error, inspect the schema, rewrite the query, and try again.
- Never invent SQL observations, row counts, schema details, or sample data. Tool results
  are the only source of truth.
- As soon as a query result answers the question, stop using tools and give the final answer.
- Summarize the final result in plain English. Do not return raw SQL unless the admin asks.

Domain notes:
- Ticket priorities are Low, Medium, High, and Critical.
- Ticket priority is stored in tickets.suggested_priority. There is no tickets.priority column.
- Ticket category is stored in tickets.category.
- Ticket summary/title text is stored in tickets.summary.
- Use tickets.created_at for created-date questions.
- Today's date for relative date questions is {today}.
- For "today", "this month", "last month", and similar relative ranges, use the
  date literal above. Do not use SQLite 'now', CURRENT_DATE, CURRENT_TIMESTAMP,
  or other database runtime date functions.
- For month filters, prefer range comparisons such as
  created_at >= date('{today}', 'start of month') and
  created_at < date('{today}', 'start of month', '+1 month').
- There is no dedicated assignee, solver, or resolution timestamp column. For solved/resolved
  questions, use status in ('Resolved', 'Closed') and explain that updated_at is the closest
  available timestamp if you rely on it.
- users.hashed_password and auth_sessions are not available to this agent.
"""

_BLOCKED_SQL_RE = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|create|truncate|replace|merge|attach|detach|"
    r"vacuum|reindex|analyze"
    r")\b",
    re.IGNORECASE,
)
_ALLOWED_START_RE = re.compile(r"^\s*(select|with|explain|pragma)\b", re.IGNORECASE)
_READ_ONLY_PRAGMA_RE = re.compile(
    r"^\s*pragma\s+(?:[a-z_][\w]*\.)?"
    r"(table_info|table_xinfo|index_list|index_info|database_list|foreign_key_list)\b",
    re.IGNORECASE,
)
_SENSITIVE_SQL_RE = re.compile(
    r"\b(auth_sessions|hashed_password|session_token)\b",
    re.IGNORECASE,
)
_COUNT_STAR_RE = re.compile(r"count\s*\(\s*\*\s*\)", re.IGNORECASE)
_RELATIVE_DATE_SQL_RE = re.compile(
    r"\b(current_date|current_time|current_timestamp)\b"
    r"|\b(?:date|datetime|strftime|julianday)\s*\([^)]*['\"]now['\"]",
    re.IGNORECASE,
)
_AGENT_STOPPED_OUTPUT = "Agent stopped due to iteration limit or time limit."
_PARSING_ERROR_MESSAGE = (
    "Invalid tool-call format. Do not invent Action, Observation, schema, or row-count text. "
    "Call one SQL tool, then wait for the real tool result."
)
_TRACE_CTX: ContextVar[dict[str, Any] | None] = ContextVar("admin_analytics_trace", default=None)
# Carries optional project_id scope for non-admin users so all tool calls are restricted.
_ANALYTICS_SCOPE: ContextVar[dict[str, Any]] = ContextVar("analytics_scope", default={})

# Tools that accept a project_id filter and should be auto-scoped.
_SCOPED_TOOL_NAMES: frozenset[str] = frozenset(
    {"count_tickets", "group_tickets", "list_tickets", "ticket_trend", "semantic_ticket_search"}
)

DateRangeName = Literal[
    "today",
    "yesterday",
    "last_7_days",
    "last_10_days",
    "last_30_days",
    "this_month",
    "last_month",
]
GroupByName = Literal[
    "status",
    "priority",
    "category",
    "user_id",
    "app_name",
    "environment",
    "project_id",
]
TrendInterval = Literal["day", "week", "month"]

GROUP_BY_COLUMNS: dict[str, str] = {
    "status": "status",
    "priority": "suggested_priority",
    "category": "category",
    "user_id": "user_id",
    "app_name": "app_name",
    "environment": "environment",
    "project_id": "project_id",
}

ANALYTICS_GRAPH_PROMPT = """You are an admin analytics agent for an IT ticketing system.

Use tools before answering factual analytics questions.

IMPORTANT: Return your final answer immediately after the FIRST tool result that answers
the question. Do not call additional tools unless the first result was clearly insufficient.

Tool guidance:
- count_tickets → exact ticket counts.
- group_tickets → breakdowns by status, priority, category, user, app, environment, or project.
- list_tickets → fetch matching ticket rows (use after counting, or when asked to show tickets).
- ticket_trend → time-series counts by day/week/month.
- semantic_ticket_search → themes, similar incidents, recurring complaints (approximate, not for exact counts).
- run_read_only_sql → last resort only, when no structured tool can express the question.

Rules:
- SQL/database tool results are the source of truth for counts, comparisons, and trends.
- Vector search is approximate. Never use vector results to produce exact counts.
- For mixed questions, use SQL for the number and vector search for qualitative examples/themes.
- Today's date is {today}. Prefer date_range values when they match the user's request.
- Be concise and explain any limitation, such as missing assignee/resolver fields.

Available ticket fields:
- status: Open, Triaged, In Progress, Resolved, Closed
- priority: stored as tickets.suggested_priority; values Low, Medium, High, Critical
- category: Bug, Feature, UI, Infra, Hardware
- created_at, updated_at, user_id, app_name, environment, project_id, summary
- text_query: case-insensitive contains search across summary, app, category, environment, and keywords
"""


class ReadOnlySQLDatabase(SQLDatabase):
    """SQLDatabase wrapper that refuses write statements before tool execution."""

    def run(self, command: str, *args: Any, **kwargs: Any) -> Any:
        logger.info("Admin analytics SQL: %s", _compact_sql(command))
        assert_read_only_sql(command)
        return super().run(command, *args, **kwargs)

    def run_no_throw(self, command: str, *args: Any, **kwargs: Any) -> str:
        try:
            assert_read_only_sql(command)
        except ValueError as exc:
            logger.warning("Blocked admin analytics SQL: %s", _compact_sql(command))
            return f"Error: {exc}"
        return super().run_no_throw(command, *args, **kwargs)


class AdminAnalyticsState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    tool_rounds: int


class TicketFilterArgs(BaseModel):
    date_range: DateRangeName | None = Field(
        default=None,
        description=(
            "Relative created_at range. Use this for requests like today, this month, "
            "last month, last 10 days, or last 30 days."
        ),
    )
    created_from: str | None = Field(
        default=None,
        description="Inclusive created_at start date in YYYY-MM-DD format.",
    )
    created_to: str | None = Field(
        default=None,
        description="Exclusive created_at end date in YYYY-MM-DD format.",
    )
    status: Literal["Open", "Triaged", "In Progress", "Resolved", "Closed"] | None = None
    priority: Literal["Low", "Medium", "High", "Critical"] | None = None
    category: Literal["Bug", "Feature", "UI", "Infra", "Hardware"] | None = None
    user_id: str | None = None
    app_name: str | None = None
    environment: str | None = None
    project_id: int | None = None
    text_query: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Case-insensitive contains search across ticket summary, app_name, "
            "category, environment, and keywords. Use for dashboard-style related searches."
        ),
    )

    @field_validator("date_range", mode="before")
    @classmethod
    def normalize_date_range(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.strip().casefold().replace("-", "_").replace(" ", "_")

    @field_validator("created_from", "created_to")
    @classmethod
    def validate_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _parse_iso_date(value)
        return value


class GroupTicketsArgs(TicketFilterArgs):
    group_by: GroupByName = Field(description="Dimension to group ticket counts by.")
    limit: int = Field(default=10, ge=1, le=25)


class ListTicketsArgs(TicketFilterArgs):
    limit: int = Field(default=10, ge=1, le=50)


class TicketTrendArgs(TicketFilterArgs):
    interval: TrendInterval = Field(default="day", description="Trend bucket size.")
    limit: int = Field(default=30, ge=1, le=60)


class SemanticTicketSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=1_000)
    status: Literal["Open", "Triaged", "In Progress", "Resolved", "Closed"] | None = None
    priority: Literal["Low", "Medium", "High", "Critical"] | None = None
    category: Literal["Bug", "Feature", "UI", "Infra", "Hardware"] | None = None
    project_id: int | None = None
    limit: int = Field(default=5, ge=1, le=10)


class ReadOnlySQLArgs(BaseModel):
    sql: str = Field(
        min_length=1,
        max_length=4_000,
        description="One read-only SQLite SELECT/WITH/EXPLAIN/PRAGMA statement.",
    )


def answer_admin_analytics_question(question: str) -> str:
    """Answer a natural-language admin analytics question using a SQL agent."""

    return run_admin_analytics_question(question)["answer"]


def run_admin_analytics_question(
    question: str,
    *,
    project_id: int | None = None,
) -> dict[str, Any]:
    """Answer an admin analytics question with LangGraph tools and trace metadata.

    Pass project_id to restrict all tool calls to a single project (non-admin users).
    """

    clean_question = " ".join(question.split())
    if not clean_question:
        return {"answer": "Please enter an analytics question.", "trace": _new_trace()}

    trace = _new_trace()
    trace["question"] = clean_question
    token = _TRACE_CTX.set(trace)
    scope_token = _ANALYTICS_SCOPE.set(
        {"project_id": project_id} if project_id is not None else {}
    )
    started = perf_counter()
    try:
        graph = _get_analytics_graph()
        state = graph.invoke(
            {"messages": [HumanMessage(content=clean_question)]},
            config={"recursion_limit": 5},  # agent→tools→agent→tools→agent = 5 max
        )
        answer = _final_message_text(state.get("messages", []))
        if answer and not trace["tools"] and _requires_grounding(clean_question):
            trace["errors"].append("Model answered a factual analytics question without tools.")
            answer = ""
        if not answer:
            answer = _answer_from_trace(clean_question, trace)
        if not answer:
            answer = _run_sql_agent_fallback(clean_question, trace)
        trace["duration_ms"] = _elapsed_ms(started)
        return {"answer": answer, "trace": _public_trace(trace)}
    except Exception as exc:
        logger.warning("Admin analytics graph failed; finalizing from trace if possible: %s", exc)
        trace["errors"].append(str(exc))
        answer = _answer_from_trace(clean_question, trace)
        if not answer:
            answer = _run_sql_agent_fallback(clean_question, trace)
        trace["duration_ms"] = _elapsed_ms(started)
        return {"answer": answer, "trace": _public_trace(trace)}
    finally:
        _TRACE_CTX.reset(token)
        _ANALYTICS_SCOPE.reset(scope_token)


def _run_sql_agent_fallback(clean_question: str, trace: dict[str, Any]) -> str:
    trace["path"] = "sql_agent_fallback"
    # Inject scope hint so the SQL agent respects the project filter.
    scope = _ANALYTICS_SCOPE.get({})
    scoped_question = clean_question
    if scope.get("project_id") is not None:
        scoped_question = f"{clean_question} [Mandatory filter: project_id = {scope['project_id']}]"

    agent = _get_sql_agent()
    result = agent.invoke({"input": scoped_question})
    output = result.get("output") if isinstance(result, dict) else result
    answer = str(output or "No answer was generated.").strip()
    if isinstance(result, dict):
        sql_result = _last_successful_sql_result(result.get("intermediate_steps"))
        if sql_result:
            sql, rows = sql_result
            return _summarize_sql_result(scoped_question, sql, rows)
        logger.warning("SQL agent produced no verified sql_db_query result.")
    if _AGENT_STOPPED_OUTPUT in answer and isinstance(result, dict):
        return _recover_from_intermediate_steps(scoped_question, result)
    if isinstance(result, dict):
        return (
            "I could not verify that answer against the database. "
            "Please try the question again or make it more specific."
        )
    return answer


def count_tickets(
    date_range: DateRangeName | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    user_id: str | None = None,
    app_name: str | None = None,
    environment: str | None = None,
    project_id: int | None = None,
    text_query: str | None = None,
) -> str:
    """Count tickets with exact SQL filters."""

    filters = _ticket_filters(
        date_range=date_range,
        created_from=created_from,
        created_to=created_to,
        status=status,
        priority=priority,
        category=category,
        user_id=user_id,
        app_name=app_name,
        environment=environment,
        project_id=project_id,
        text_query=text_query,
    )
    where_sql, params = _ticket_where_clause(filters)
    rows = _run_sql(f"select count(*) from tickets{where_sql}", params, purpose="count_tickets")
    parsed = _rows_from_sql_result(rows)
    count = parsed[0][0] if parsed else 0
    return _json({"count": count, "filters": filters})


def group_tickets(
    group_by: GroupByName,
    date_range: DateRangeName | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    user_id: str | None = None,
    app_name: str | None = None,
    environment: str | None = None,
    project_id: int | None = None,
    text_query: str | None = None,
    limit: int = 10,
) -> str:
    """Group ticket counts by a supported dimension."""

    column = GROUP_BY_COLUMNS[group_by]
    filters = _ticket_filters(
        date_range=date_range,
        created_from=created_from,
        created_to=created_to,
        status=status,
        priority=priority,
        category=category,
        user_id=user_id,
        app_name=app_name,
        environment=environment,
        project_id=project_id,
        text_query=text_query,
    )
    where_sql, params = _ticket_where_clause(filters)
    params["limit"] = limit
    sql = (
        f"select coalesce(cast({column} as text), 'unknown') as bucket, count(*) as count "
        f"from tickets{where_sql} group by bucket order by count desc, bucket asc limit :limit"
    )
    rows = _rows_from_sql_result(_run_sql(sql, params, purpose="group_tickets"))
    return _json(
        {
            "group_by": group_by,
            "filters": filters,
            "rows": [{"bucket": row[0], "count": row[1]} for row in rows],
        }
    )


def list_tickets(
    date_range: DateRangeName | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    user_id: str | None = None,
    app_name: str | None = None,
    environment: str | None = None,
    project_id: int | None = None,
    text_query: str | None = None,
    limit: int = 10,
) -> str:
    """Return matching ticket rows with the same filters used for counts."""

    filters = _ticket_filters(
        date_range=date_range,
        created_from=created_from,
        created_to=created_to,
        status=status,
        priority=priority,
        category=category,
        user_id=user_id,
        app_name=app_name,
        environment=environment,
        project_id=project_id,
        text_query=text_query,
    )
    where_sql, params = _ticket_where_clause(filters)
    count_rows = _rows_from_sql_result(
        _run_sql(f"select count(*) from tickets{where_sql}", params, purpose="list_tickets_count")
    )
    total_count = count_rows[0][0] if count_rows else 0
    list_params = dict(params)
    list_params["limit"] = limit
    sql = (
        "select id, summary, status, suggested_priority, category, app_name, environment, "
        f"user_id, created_at, updated_at from tickets{where_sql} "
        "order by created_at desc limit :limit"
    )
    rows = _rows_from_sql_result(_run_sql(sql, list_params, purpose="list_tickets"))
    return _json(
        {
            "filters": filters,
            "total_count": total_count,
            "result_count": len(rows),
            "limit": limit,
            "rows": [
                {
                    "ticket_id": row[0],
                    "summary": row[1],
                    "status": row[2],
                    "priority": row[3],
                    "category": row[4],
                    "app_name": row[5],
                    "environment": row[6],
                    "user_id": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                }
                for row in rows
            ],
        }
    )


def ticket_trend(
    interval: TrendInterval = "day",
    date_range: DateRangeName | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    user_id: str | None = None,
    app_name: str | None = None,
    environment: str | None = None,
    project_id: int | None = None,
    text_query: str | None = None,
    limit: int = 30,
) -> str:
    """Return ticket count trend buckets by day, week, or month."""

    bucket_expr = {
        "day": "date(created_at)",
        "week": "strftime('%Y-W%W', created_at)",
        "month": "strftime('%Y-%m', created_at)",
    }[interval]
    filters = _ticket_filters(
        date_range=date_range,
        created_from=created_from,
        created_to=created_to,
        status=status,
        priority=priority,
        category=category,
        user_id=user_id,
        app_name=app_name,
        environment=environment,
        project_id=project_id,
        text_query=text_query,
    )
    where_sql, params = _ticket_where_clause(filters)
    params["limit"] = limit
    sql = (
        f"select {bucket_expr} as bucket, count(*) as count from tickets{where_sql} "
        "group by bucket order by bucket desc limit :limit"
    )
    rows = _rows_from_sql_result(_run_sql(sql, params, purpose="ticket_trend"))
    return _json(
        {
            "interval": interval,
            "filters": filters,
            "rows": [{"bucket": row[0], "count": row[1]} for row in rows],
        }
    )


def semantic_ticket_search(
    query: str,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    project_id: int | None = None,
    limit: int = 5,
) -> str:
    """Search ticket vectors for semantic themes, examples, and similar incidents."""

    started = perf_counter()
    results = search_ticket_vectors(
        query,
        project_id=project_id,
        status=status,
        priority=priority,
        limit=max(limit * 3, limit),
    )
    if category:
        results = [
            result
            for result in results
            if str(result.metadata.get("category") or "").casefold() == category.casefold()
        ]
    results = results[:limit]
    _record_vector_query(
        query=query,
        filters={
            "status": status,
            "priority": priority,
            "category": category,
            "project_id": project_id,
            "limit": limit,
        },
        result_count=len(results),
        duration_ms=_elapsed_ms(started),
    )
    return _json(
        {
            "query": query,
            "result_count": len(results),
            "results": [
                {
                    "ticket_id": result.ticket_id,
                    "score": result.score,
                    "summary": result.summary,
                    "status": result.metadata.get("status"),
                    "priority": result.metadata.get("priority"),
                    "category": result.metadata.get("category"),
                    "created_at": result.metadata.get("created_at"),
                    "context": _shorten_text(result.content, max_chars=900),
                }
                for result in results
            ],
        }
    )


def describe_analytics_schema() -> str:
    """Describe the safe analytics schema and filterable fields."""

    return _json(
        {
            "tables": READ_ONLY_TABLES,
            "ticket_fields": {
                "status": [status.value for status in TicketStatus],
                "priority": {
                    "column": "suggested_priority",
                    "values": [priority.value for priority in Priority],
                },
                "category": [category.value for category in TicketCategory],
                "dates": ["created_at", "updated_at"],
                "dimensions": list(GROUP_BY_COLUMNS),
                "text_query": "Case-insensitive contains search across summary, app, category, environment, and keywords.",
                "semantic_search": "Use semantic_ticket_search for themes and similar tickets.",
            },
        }
    )


def run_read_only_sql(sql: str) -> str:
    """Run one guarded read-only SQL statement for questions not covered by structured tools."""

    result = _run_sql(sql, {}, purpose="run_read_only_sql")
    return _json({"rows": _rows_from_sql_result(result), "raw": result})


@lru_cache(maxsize=1)
def _get_admin_tools() -> list[BaseTool]:
    return [
        StructuredTool.from_function(
            count_tickets,
            name="count_tickets",
            description="Count tickets with exact filters. Use for how many/count questions.",
            args_schema=TicketFilterArgs,
        ),
        StructuredTool.from_function(
            group_tickets,
            name="group_tickets",
            description="Group exact ticket counts by one dimension.",
            args_schema=GroupTicketsArgs,
        ),
        StructuredTool.from_function(
            list_tickets,
            name="list_tickets",
            description="Return matching ticket rows for exact filters with a bounded limit.",
            args_schema=ListTicketsArgs,
        ),
        StructuredTool.from_function(
            ticket_trend,
            name="ticket_trend",
            description="Return exact ticket count trends by day, week, or month.",
            args_schema=TicketTrendArgs,
        ),
        StructuredTool.from_function(
            semantic_ticket_search,
            name="semantic_ticket_search",
            description=(
                "Semantic Pinecone search over ticket summaries, conversations, keywords, "
                "and suggested fixes. Use for themes/similar examples, not exact counts."
            ),
            args_schema=SemanticTicketSearchArgs,
        ),
        StructuredTool.from_function(
            describe_analytics_schema,
            name="describe_analytics_schema",
            description="Show available analytics fields, tables, and filter values.",
        ),
        StructuredTool.from_function(
            run_read_only_sql,
            name="run_read_only_sql",
            description="Guarded SQL fallback for read-only analytics questions.",
            args_schema=ReadOnlySQLArgs,
        ),
    ]


@lru_cache(maxsize=1)
def _get_analytics_graph() -> Any:
    workflow = StateGraph(AdminAnalyticsState)
    workflow.add_node("agent", _analytics_agent_node)
    workflow.add_node("tools", _analytics_tools_node)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges(
        "agent",
        _route_after_analytics_agent,
        {"tools": "tools", "__end__": END},
    )
    workflow.add_edge("tools", "agent")
    return workflow.compile()


def _analytics_agent_node(state: AdminAnalyticsState) -> dict[str, Any]:
    scope = _ANALYTICS_SCOPE.get({})
    scope_instruction = ""
    if scope.get("project_id") is not None:
        scope_instruction = (
            f"\n\nMANDATORY SCOPE: You are answering for a project-scoped user. "
            f"ALL tool calls MUST include project_id={scope['project_id']}. "
            f"Never return data from other projects."
        )
    prompt = ANALYTICS_GRAPH_PROMPT.replace("{today}", _today_iso()) + scope_instruction
    llm = get_chat_model().bind_tools(_get_admin_tools())
    messages = [
        SystemMessage(content=prompt),
        *state.get("messages", []),
    ]
    return {"messages": [llm.invoke(messages)]}


def _analytics_tools_node(state: AdminAnalyticsState) -> dict[str, Any]:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}

    tools = {tool.name: tool for tool in _get_admin_tools()}
    messages: list[ToolMessage] = []
    scope = _ANALYTICS_SCOPE.get({})
    for call in last.tool_calls:
        name = call["name"]
        args = dict(call.get("args") or {})
        # Enforce project scope at the tool-call level (reliable fallback in case
        # the LLM omits the project_id filter despite the prompt instruction).
        if scope.get("project_id") is not None and name in _SCOPED_TOOL_NAMES:
            args["project_id"] = scope["project_id"]
        tool_id = call["id"]
        started = perf_counter()
        selected = tools.get(name)
        ok = selected is not None
        if selected is None:
            content = f"Unknown tool: {name}"
        else:
            try:
                content = selected.invoke(args)
            except Exception as exc:
                ok = False
                content = f"Tool error: {exc}"
        _record_tool_call(
            name=name,
            args=args,
            ok=ok,
            duration_ms=_elapsed_ms(started),
            preview=str(content)[:700],
            output=str(content)[:4000],
        )
        messages.append(ToolMessage(content=str(content), tool_call_id=tool_id, name=name))
    return {"messages": messages, "tool_rounds": state.get("tool_rounds", 0) + 1}


def _route_after_analytics_agent(state: AdminAnalyticsState) -> Literal["tools", "__end__"]:
    last = state["messages"][-1]
    # Allow at most 2 tool rounds (handles count+list combos); most queries need only 1.
    if isinstance(last, AIMessage) and last.tool_calls and state.get("tool_rounds", 0) < 2:
        return "tools"
    return "__end__"


@lru_cache(maxsize=1)
def _get_sql_agent() -> Any:
    llm = get_chat_model()
    db = _get_analytics_db()
    toolkit = SQLDatabaseToolkit(db=db, llm=llm)
    return create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        agent_type="tool-calling",
        prefix=SQL_AGENT_PREFIX.replace("{today}", _today_iso()),
        top_k=25,
        max_iterations=5,          # was 15 — hard cap to stay within 30 s budget
        max_execution_time=20,     # was 120 s — fail fast and surface the structured fallback
        verbose=False,
        agent_executor_kwargs={
            "handle_parsing_errors": _PARSING_ERROR_MESSAGE,
            "return_intermediate_steps": True,
            "trim_intermediate_steps": 8,
        },
    )


def _recover_from_intermediate_steps(question: str, result: dict[str, Any]) -> str:
    sql_result = _last_successful_sql_result(result.get("intermediate_steps"))
    if sql_result is None:
        logger.warning("SQL agent stopped before producing a usable query result.")
        return (
            "I could not complete that analytics question before the agent limit was reached. "
            "Try asking it with a narrower time range, status, category, priority, or user."
        )

    sql, rows = sql_result
    logger.warning("SQL agent reached its limit; summarizing the last SQL result.")
    return _summarize_sql_result(question, sql, rows)


def _summarize_sql_result(question: str, sql: str, rows: str) -> str:
    scalar_count_answer = _scalar_count_answer(question, rows)
    if scalar_count_answer:
        return scalar_count_answer

    try:
        response = get_chat_model().invoke(
            [
                SystemMessage(
                    content=(
                        "You summarize SQL analytics results for an admin. "
                        "Use only the provided question, SQL, and result. "
                        "Answer in plain English and be concise."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Question: {question}\n"
                        f"SQL: {sql}\n"
                        f"Result: {_shorten_text(rows, max_chars=4000)}"
                    )
                ),
            ]
        )
        content = getattr(response, "content", response)
        final_answer = str(content or "").strip()
        if final_answer:
            return final_answer
    except Exception as exc:
        logger.warning("SQL agent fallback summarization failed: %s", exc)

    return f"The query completed but the agent did not finalize the response. Result: {rows}"


def _scalar_count_answer(question: str, rows: str) -> str | None:
    try:
        parsed = ast.literal_eval(rows)
    except (SyntaxError, ValueError):
        return None

    if (
        isinstance(parsed, list)
        and len(parsed) == 1
        and isinstance(parsed[0], tuple)
        and len(parsed[0]) == 1
        and isinstance(parsed[0][0], int)
    ):
        count = parsed[0][0]
        if "ticket" in question.casefold():
            return f"There were {count} matching tickets."
        return f"The result is {count}."
    return None


def _last_successful_sql_result(
    intermediate_steps: Any,
) -> tuple[str, str] | None:
    if not isinstance(intermediate_steps, list):
        return None

    for action, observation in reversed(intermediate_steps):
        if getattr(action, "tool", "") != "sql_db_query":
            continue
        output = str(observation or "").strip()
        if not output or output.casefold().startswith("error:"):
            continue
        return _tool_input_to_sql(getattr(action, "tool_input", "")), output
    return None


def _tool_input_to_sql(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        return str(tool_input.get("query") or tool_input)
    return str(tool_input)


def _shorten_text(value: str, *, max_chars: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_chars:
        return clean
    return f"{clean[:max_chars].rstrip()}..."


def _ticket_filters(
    *,
    date_range: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    user_id: str | None = None,
    app_name: str | None = None,
    environment: str | None = None,
    project_id: int | None = None,
    text_query: str | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if date_range:
        start, end = _date_range_bounds(date_range)
        filters["created_from"] = start.isoformat()
        filters["created_to"] = end.isoformat()
        filters["date_range"] = date_range
    else:
        if created_from:
            filters["created_from"] = _parse_iso_date(created_from).isoformat()
        if created_to:
            filters["created_to"] = _parse_iso_date(created_to).isoformat()

    if status:
        filters["status"] = _enum_value(status, TicketStatus)
    if priority:
        filters["priority"] = _enum_value(priority, Priority)
    if category:
        filters["category"] = _enum_value(category, TicketCategory)
    if user_id:
        filters["user_id"] = user_id
    if app_name:
        filters["app_name"] = app_name
    if environment:
        filters["environment"] = environment
    if project_id is not None:
        filters["project_id"] = project_id
    if text_query and text_query.strip():
        filters["text_query"] = text_query.strip()
    return filters


def _ticket_where_clause(filters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    field_map = {
        "created_from": ("created_at >= :created_from", "created_from"),
        "created_to": ("created_at < :created_to", "created_to"),
        "status": ("status = :status", "status"),
        "priority": ("suggested_priority = :priority", "priority"),
        "category": ("category = :category", "category"),
        "user_id": ("user_id = :user_id", "user_id"),
        "app_name": ("lower(app_name) = lower(:app_name)", "app_name"),
        "environment": ("environment = :environment", "environment"),
        "project_id": ("project_id = :project_id", "project_id"),
    }
    for key, (clause, param_name) in field_map.items():
        if key not in filters:
            continue
        clauses.append(clause)
        params[param_name] = filters[key]
    if "text_query" in filters:
        clauses.append(
            "("
            "lower(coalesce(summary, '') || ' ' || coalesce(app_name, '') || ' ' || "
            "coalesce(category, '') || ' ' || coalesce(environment, '') || ' ' || "
            "coalesce(cast(keywords as text), '')) like :text_query escape '\\'"
            ")"
        )
        params["text_query"] = _contains_pattern(str(filters["text_query"]).casefold())

    if not clauses:
        return "", params
    return f" where {' and '.join(clauses)}", params


def _run_sql(sql: str, params: dict[str, Any], *, purpose: str) -> str:
    started = perf_counter()
    result = _get_analytics_db().run_no_throw(sql, parameters=params)
    rows = _rows_from_sql_result(result)
    _record_sql(
        statement=sql,
        params=params,
        purpose=purpose,
        duration_ms=_elapsed_ms(started),
        row_count=len(rows),
        result_preview=str(result)[:700],
    )
    if str(result).casefold().startswith("error:"):
        raise ValueError(str(result))
    return str(result)


def _rows_from_sql_result(result: Any) -> list[tuple[Any, ...]]:
    if not result:
        return []
    try:
        parsed = ast.literal_eval(str(result))
    except (SyntaxError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    rows: list[tuple[Any, ...]] = []
    for row in parsed:
        if isinstance(row, tuple):
            rows.append(row)
        elif isinstance(row, list):
            rows.append(tuple(row))
    return rows


def _date_range_bounds(range_name: str) -> tuple[date, date]:
    today = _parse_iso_date(_today_iso())
    if range_name == "today":
        return today, today + timedelta(days=1)
    if range_name == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, today
    if range_name == "last_7_days":
        return today - timedelta(days=7), today + timedelta(days=1)
    if range_name == "last_10_days":
        return today - timedelta(days=10), today + timedelta(days=1)
    if range_name == "last_30_days":
        return today - timedelta(days=30), today + timedelta(days=1)
    if range_name == "this_month":
        month_start = today.replace(day=1)
        return month_start, _add_one_month(month_start)
    if range_name == "last_month":
        this_month = today.replace(day=1)
        last_month_end = this_month
        last_month_start = (this_month - timedelta(days=1)).replace(day=1)
        return last_month_start, last_month_end
    raise ValueError(f"Unsupported date_range={range_name!r}.")


def _add_one_month(value: date) -> date:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Expected YYYY-MM-DD date, got {value!r}.") from exc


def _enum_value(value: str, enum_type: Any) -> str:
    normalized = value.casefold()
    for item in enum_type:
        if item.value.casefold() == normalized:
            return item.value
    allowed = ", ".join(item.value for item in enum_type)
    raise ValueError(f"Unsupported value {value!r}. Expected one of: {allowed}.")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, default=str)


def _new_trace() -> dict[str, Any]:
    return {
        "path": "langgraph_tools",
        "question": "",
        "duration_ms": 0,
        "tools": [],
        "sql": [],
        "vector": [],
        "errors": [],
    }


def _public_trace(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": trace.get("path"),
        "duration_ms": trace.get("duration_ms", 0),
        "tools": trace.get("tools", []),
        "sql": trace.get("sql", []),
        "vector": trace.get("vector", []),
        "errors": trace.get("errors", []),
    }


def _record_tool_call(
    *,
    name: str,
    args: dict[str, Any],
    ok: bool,
    duration_ms: int,
    preview: str,
    output: str,
) -> None:
    trace = _TRACE_CTX.get()
    if trace is None:
        return
    trace["tools"].append(
        {
            "name": name,
            "args": _safe_trace_value(args),
            "ok": ok,
            "duration_ms": duration_ms,
            "preview": preview,
            "output": output,
        }
    )


def _record_sql(
    *,
    statement: str,
    params: dict[str, Any],
    purpose: str,
    duration_ms: int,
    row_count: int,
    result_preview: str,
) -> None:
    trace = _TRACE_CTX.get()
    if trace is None:
        return
    trace["sql"].append(
        {
            "purpose": purpose,
            "statement": _compact_sql(statement),
            "params": _safe_trace_value(params),
            "row_count": row_count,
            "duration_ms": duration_ms,
            "result_preview": result_preview,
        }
    )


def _record_vector_query(
    *,
    query: str,
    filters: dict[str, Any],
    result_count: int,
    duration_ms: int,
) -> None:
    trace = _TRACE_CTX.get()
    if trace is None:
        return
    trace["vector"].append(
        {
            "query": query,
            "filters": _safe_trace_value(filters),
            "result_count": result_count,
            "duration_ms": duration_ms,
        }
    )


def _safe_trace_value(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except TypeError:
        return str(value)


def _final_message_text(messages: list[BaseMessage]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    content = getattr(last, "content", "")
    if isinstance(content, list):
        return "\n".join(str(part) for part in content).strip()
    return str(content or "").strip()


def _requires_grounding(question: str) -> bool:
    lowered = question.casefold()
    return "ticket" in lowered and any(
        term in lowered
        for term in (
            "how many",
            "count",
            "breakdown",
            "break down",
            "compare",
            "trend",
            "top",
            "most",
            "least",
            "show",
            "list",
            "created",
            "resolved",
            "closed",
            "open",
            "priority",
            "category",
            "status",
            "similar",
            "theme",
            "saying",
        )
    )


def _answer_from_trace(question: str, trace: dict[str, Any]) -> str:
    direct_answer = _direct_answer_from_trace(trace)
    if direct_answer:
        return direct_answer

    tool_outputs = [
        {
            "tool": tool.get("name"),
            "ok": tool.get("ok"),
            "args": tool.get("args"),
            "output": tool.get("output") or tool.get("preview"),
        }
        for tool in trace.get("tools", [])
        if tool.get("ok")
    ]
    if not tool_outputs:
        return ""

    try:
        response = get_chat_model().invoke(
            [
                SystemMessage(
                    content=(
                        "Summarize an admin analytics answer using only these verified "
                        "tool outputs. Do not invent counts or examples. Be concise."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Question: {question}\n"
                        f"Tool outputs: {_shorten_text(_json(tool_outputs), max_chars=6000)}"
                    )
                ),
            ]
        )
        return str(getattr(response, "content", response) or "").strip()
    except Exception as exc:
        logger.warning("Trace summarization failed: %s", exc)
        return ""


def _direct_answer_from_trace(trace: dict[str, Any]) -> str:
    has_exact_data = any(
        tool.get("ok")
        and tool.get("name")
        in {"run_read_only_sql", "count_tickets", "group_tickets", "list_tickets", "ticket_trend"}
        for tool in trace.get("tools", [])
    )
    for tool in reversed(trace.get("tools", [])):
        if not tool.get("ok"):
            continue
        try:
            payload = json.loads(tool.get("output") or tool.get("preview") or "{}")
        except json.JSONDecodeError:
            continue

        name = tool.get("name")
        if name == "count_tickets" and "count" in payload:
            return f"There were {payload['count']} matching tickets."
        if name == "group_tickets":
            rows = payload.get("rows") or []
            if rows:
                group_by = payload.get("group_by", "field")
                parts = ", ".join(f"{row['bucket']}: {row['count']}" for row in rows)
                return f"Ticket count by {group_by}: {parts}."
        if name == "list_tickets":
            rows = payload.get("rows") or []
            total_count = payload.get("total_count", len(rows))
            if rows:
                parts = ", ".join(
                    f"#{row['ticket_id']} {row['summary']}" for row in rows[:10]
                )
                return f"There were {total_count} matching tickets. Showing {len(rows)}: {parts}."
            return "There were 0 matching tickets."
        if name == "ticket_trend":
            rows = payload.get("rows") or []
            if rows:
                interval = payload.get("interval", "period")
                parts = ", ".join(f"{row['bucket']}: {row['count']}" for row in rows)
                return f"Ticket trend by {interval}: {parts}."
        if name == "semantic_ticket_search" and payload.get("result_count") == 0 and not has_exact_data:
            return "I did not find semantically similar ticket examples in the vector index."
    return ""


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def _contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@lru_cache(maxsize=1)
def _get_analytics_db() -> ReadOnlySQLDatabase:
    settings = get_settings()
    logger.info("Admin analytics database: %s", _database_label(settings.database_url))
    engine = create_engine(
        settings.database_url,
        future=True,
        **_engine_kwargs(settings.database_url),
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_query_only(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        if settings.database_url.startswith("sqlite"):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA query_only=ON")
            cursor.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _prevent_writes(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        assert_read_only_sql(statement, allow_wildcard=True)

    db = ReadOnlySQLDatabase(
        engine=engine,
        include_tables=READ_ONLY_TABLES,
        custom_table_info=CUSTOM_TABLE_INFO,
        sample_rows_in_table_info=2,
        max_string_length=500,
    )
    logger.info("Admin analytics ticket count: %s", db.run_no_throw("select count(*) from tickets"))
    return db


def assert_read_only_sql(sql: str, *, allow_wildcard: bool = False) -> None:
    """Reject SQL that is not safe for read-only analytics."""

    normalized = _strip_comments(sql).strip()
    if not normalized:
        raise ValueError("Only read-only SQL statements are allowed.")

    sensitive_subject = _SENSITIVE_SQL_RE.search(normalized)
    if sensitive_subject:
        raise ValueError(f"Access to {sensitive_subject.group(1)} is not allowed.")

    if _RELATIVE_DATE_SQL_RE.search(normalized):
        raise ValueError(
            "SQLite runtime date functions are not allowed for analytics. "
            f"Use the current date literal {_today_iso()} for relative date ranges."
        )

    sql_without_literals = _strip_single_quoted_literals(normalized)
    if ";" in sql_without_literals.rstrip(";"):
        raise ValueError("Only one SQL statement is allowed at a time.")

    if not allow_wildcard:
        sql_without_count_star = _COUNT_STAR_RE.sub("count_rows", sql_without_literals)
        if "*" in sql_without_count_star:
            raise ValueError("Wildcard column selection is not allowed.")

    blocked = _BLOCKED_SQL_RE.search(sql_without_literals)
    if blocked:
        raise ValueError(f"{blocked.group(1).upper()} statements are not allowed.")

    if normalized.casefold().startswith("pragma"):
        if _READ_ONLY_PRAGMA_RE.match(normalized) is None:
            raise ValueError("Only read-only schema PRAGMA statements are allowed.")
        return

    if _ALLOWED_START_RE.match(normalized) is None:
        raise ValueError("Only SELECT-style read-only SQL statements are allowed.")


def _engine_kwargs(database_url: str) -> dict[str, Any]:
    settings = get_settings()
    if database_url.startswith("sqlite"):
        return {
            "connect_args": {
                "check_same_thread": False,
                "timeout": settings.sqlite_connect_timeout,
            }
        }
    return {}


def _database_label(database_url: str) -> str:
    settings = get_settings()
    if settings.sqlite_path:
        return str(settings.sqlite_path.resolve())
    return database_url


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def _strip_comments(sql: str) -> str:
    without_block_comments = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--.*?$", " ", without_block_comments, flags=re.MULTILINE)


def _strip_single_quoted_literals(sql: str) -> str:
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def _compact_sql(sql: str) -> str:
    return " ".join(sql.split())
