# Architecture

Capstone ITS v2 is a FastAPI application with a user-facing chat triage agent and an admin dashboard. The backend uses LangChain for model/retrieval calls, LangGraph for the stateful workflow, Pydantic for every LLM structured output, SQLite for tickets and graph checkpoints, and Pinecone for vector search.

## Runtime Components

- **FastAPI** serves the chat page, admin dashboard, static assets, and JSON APIs.
- **LangGraph** owns the multi-turn workflow and persists thread state with `langgraph-checkpoint-sqlite`.
- **LangChain** wraps the configured chat model, Pinecone retrieval, and FlashRank reranking.
- **Pydantic** validates graph state objects, ticket metadata, guardrail results, classifications, requirement assessments, and final answer validation.
- **SQLite + SQLAlchemy** stores tickets, ticket messages, KB links, duplicate relations, and LangGraph checkpoints.
- **Pinecone** stores KB vectors in `its-knowledge-base` and ticket vectors in `its-tickets`.

## API Surface

- `POST /api/chat`: accepts `message`, `conversation_id` or `thread_id`, `user_id`, `app_name`, `environment`, and `clearance`; returns the assistant response, route, optional ticket id, and KB citations.
- `GET /api/tickets`: returns flattened queue rows for the admin dashboard.
- `GET /api/tickets/{id}`: returns one ticket, conversation history, guardrail details, and raw graph context.
- `GET /api/tickets/{id}/insights`: returns KB references, duplicate candidates, suggested priority, and suggested fixes.
- `GET /api/health`: basic service probe.

## SQL Models

Defined in `app/db.py`:

- `tickets`: status, timestamps, user/thread ids, app/environment, clearance, category, priority, summary, keywords, serialized Pydantic intelligence/resolution/guardrail/conversation/raw context.
- `ticket_messages`: normalized message audit trail per ticket.
- `ticket_kb_links`: linked KB articles with relevance and clearance metadata.
- `duplicate_ticket_links`: ticket-to-ticket duplicate relationship candidates.
- LangGraph checkpoint tables are managed by `SqliteSaver` in `data/langgraph_checkpoints.sqlite`.
- Ticket vector documents are stored in the Pinecone index `its-tickets`; SQLite remains the source of truth.

## Pydantic State

Defined in `app/schemas.py`:

- `HelpdeskGraphState` is a `TypedDict` with primitive user/session fields plus Pydantic values: `ChatMessage`, `RequirementAssessment`, `IssueClassification`, `GuardrailDecision`, `SelfResolutionAnswer`, `AnswerValidation`, `TicketRead`, and `ChatTurnResult`.
- `TicketCreate` and `TicketRead` strictly define core system data, contextual data, AI-generated intelligence, and resolution data.
- `TicketIntelligence` captures category, suggested priority, summary, keywords, and confidence.

## LangGraph Workflow

Defined in `app/graph.py`:

1. `input_guardrails`: deterministic prompt-injection and unauthorized-access checks.
2. `assess_requirements`: structured LLM assessment; asks one targeted follow-up when vague.
3. `ask_follow_up`: ends the turn with a dynamic question when under the max turn limit.
4. `classify_and_guard`: structured LLM category classification and safety/authorization assessment.
5. `retrieve_and_answer`: dynamic metadata-filtered hybrid RAG, reranking, and structured self-resolution answer.
6. `validate_answer`: structured LLM output validation plus deterministic PII/secrets redaction.
7. `finalize_resolution`: returns safe self-service guidance with citations.
8. `create_ticket`: extracts structured ticket intelligence, finds duplicate candidates, persists the ticket, and returns the ticket id.

Conditional edges:

- Unsafe input goes directly to `create_ticket`.
- Unclear requirements go to `ask_follow_up` until `MAX_REQUIREMENT_TURNS`; then escalation.
- Low-risk, authorized requests go to RAG.
- High-risk, access-changing, no-KB, low-confidence, or unsafe-output paths go to `create_ticket`.
- Safe validated answers end at `finalize_resolution`.

## Guardrails

- HTTP/Pydantic validation limits payload shape and size.
- `app/guardrails.py` detects prompt injection, hidden prompt extraction, privileged access requests, credential disclosure, and sensitive output patterns.
- LLM guardrails run in graph generation steps with `.with_structured_output(GuardrailDecision)` and `.with_structured_output(AnswerValidation)`.
- Output validation sanitizes emails, phone numbers, SSNs, API keys, passwords, tokens, and private keys before a chat response is returned.
- Retrieval filtering prevents standard users from seeing internal or restricted KB metadata.

## RAG Pipeline

Defined in `app/rag.py` and `scripts/ingest_kb.py`:

1. Load Markdown files from `kb/` with LangChain `DirectoryLoader` and `TextLoader`.
2. Parse front matter only to normalize metadata: `category`, `clearance` or `clearance_level`, `app_name`, and `environment`.
3. Split content with LangChain `MarkdownHeaderTextSplitter` and `MarkdownTextSplitter`.
4. Store chunks in Pinecone index `its-knowledge-base` using the embedding model returned by `app.llm.get_embedding_model()`.
5. At query time, build a Pinecone metadata filter from user clearance, category, app, and environment before similarity search.
6. Rerank with LangChain's `FlashrankRerank`.

## Runbook

```bash
uv sync
cp .env.example .env
uv run python scripts/ingest_kb.py
uv run python scripts/ingest_tickets.py
uv run uvicorn app.main:app --reload
```

Model construction is centralized in `app/llm.py`.

- OpenAI chat and embeddings: `LLM_PROVIDER=openai`, `EMBEDDING_PROVIDER=openai`.
- Ollama chat: `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=...`, `OLLAMA_BASE_URL=...`.
- Hugging Face embeddings: `EMBEDDING_PROVIDER=huggingface` after installing the `huggingface` extra.
- Pinecone: set `PINECONE_API_KEY`; defaults are `PINECONE_KB_INDEX_NAME=its-knowledge-base` and `PINECONE_TICKET_INDEX_NAME=its-tickets`.
