# AI Assistant Workflow

## Overview

The assistant runs as two separate LangGraph pipelines:

- **Helpdesk graph** (`app/graph.py`) — user-facing ReAct loop.
- **Analytics graph** (`app/admin_analytics.py`) — admin analytics delegation.

FastAPI resolves authenticated identity, role, clearance, project membership,
and thread scope before invoking either graph.

## Helpdesk Graph

### Flow

```mermaid
flowchart TD
    A[User message] --> B[FastAPI: auth / project / thread]
    B --> C[guardrail node]
    C -->|Unsafe| D[Create security ticket → blocked response → END]
    C -->|Safe| E[agent node]
    E --> F{Tool calls?}
    F -->|No| G[Final answer]
    F -->|Yes| H[tools node]
    H --> I{Direct response tool?}
    I -->|Yes| G
    I -->|No, rounds < 4| E
    I -->|No, rounds >= 4| G
    G --> J[Redact secrets / PII]
    J --> K[Infer route]
    K --> L[Persist chat turn]
    L --> M[Return API response]
```

### Registered Tools

| Tool | Purpose |
|---|---|
| `search_knowledge_base` | Vector search over KB articles and runbooks |
| `find_tickets` | Ticket lookup by keyword, topic, or symptoms (`semantic=true`) |
| `analyze_ticket_data` | Delegates to analytics graph for counts, trends, breakdowns |
| `retrieve_ticket_comments` | Vector search over ticket comments |
| `create_helpdesk_ticket` | Creates a ticket and returns a direct response |
| `render_chart` | Stores chart config for frontend rendering |

### Guardrails

The guardrail node blocks prompt injection, credential disclosure, and privilege
escalation attempts. Blocked turns always create a critical security ticket.

### Route Labels

| Route | Condition |
|---|---|
| `blocked` | Guardrail fired |
| `ticket_created` | `create_helpdesk_ticket` was called |
| `self_resolution` | KB references attached |
| `follow_up` | All other turns |

## Analytics Graph

Called via `analyze_ticket_data` (helpdesk tool) or `POST /api/admin/analytics`.

### Flow

```mermaid
flowchart TD
    A[Analytics question] --> B{Direct structured answer?}
    B -->|Yes| C[Return answer]
    B -->|No| D[retrieve_schemas node]
    D --> E[agent node]
    E --> F{Tool calls? First round only}
    F -->|Yes| G[tools node: inject project_id, run SQL tools]
    G --> E
    F -->|No| H{Answer produced?}
    H -->|Yes| C
    H -->|No| I[Recover from trace → SQL-agent fallback]
    I --> C
```

### Analytics Tools

`count_tickets`, `group_tickets`, `list_tickets`, `ticket_trend`,
`semantic_ticket_search`, `describe_analytics_schema`, `run_read_only_sql`

Structured tools receive enforced `project_id` injection for project-scoped
users. All SQL execution is read-only.

## Chart Response Flow

Charts are produced by the helpdesk graph, not the analytics graph.

```mermaid
flowchart TD
    A[User asks for breakdown or trend] --> B[analyze_ticket_data returns evidence]
    B --> C{Numeric rows useful for a chart?}
    C -->|No| D[Markdown answer]
    C -->|Yes| E[render_chart stores ChartConfiguration]
    E --> F[AgentResponse returned to frontend]
    F --> G[Frontend renders DynamicChart]
```

## Persistence

| Store | Contents |
|---|---|
| `chat_messages` | User and assistant messages, optional `agent_response` JSON |
| `tickets` | Tickets with category, priority, keywords, project |
| `langgraph_checkpoints.sqlite` | LangGraph state checkpoints |

Deleting chat history removes `chat_messages` rows only — LangGraph checkpoint
rows for the same thread are not cleared.

## Known Gaps

- Non-admin arbitrary SQL paths are read-only but not project-rewritten.
- Ticket/comment vector retrieval has clearance-filtering inconsistencies.
- Ticket insights retrieve KB context as `internal` clearance regardless of requester clearance.
- Chat thread deletion does not clear LangGraph checkpoints.
