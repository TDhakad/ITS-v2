# Capstone ITS v2

Conversational IT helpdesk and ticketing system built with FastAPI, LangChain, LangGraph, Pydantic, SQLite, and Pinecone.

## Quick Start

```bash
uv sync
cp .env.example .env
uv run python scripts/ingest_kb.py
uv run python scripts/ingest_tickets.py
uv run uvicorn app.main:app --reload
```

Run the lightweight background worker in a second shell when you want queued
vector indexing and ticket insight refresh jobs to process:

```bash
uv run python scripts/run_worker.py
```

Open `http://127.0.0.1:8000` for the backend-served UI. When a production
frontend build is present, FastAPI serves the React app; otherwise it falls back
to the legacy templates. The React app's active routes are `/`, `/assistant`,
and `/tickets/:ticketId`.

## React frontend

The React UI lives in `frontend/` and calls the existing FastAPI routes under
`/api` and `/auth`. It uses Vite, React Router, Recharts, React Markdown, and
Lucide; it does not use Refine.js.

```bash
cd frontend
npm install
npm run dev
```

Run the FastAPI server on `http://127.0.0.1:8000`; Vite serves the UI on `http://127.0.0.1:5173` and proxies API/auth requests to FastAPI.

For a production build:

```bash
cd frontend
npm run build
cd ..
uv run uvicorn app.main:app --reload
```

When `frontend/dist/index.html` exists, FastAPI serves the React shell for `/`
and client fallback routes while keeping the API endpoints unchanged. `/login`,
`/register`, and `/admin` are backend entrypoints/fallbacks; the current React
routes are `/`, `/assistant`, and `/tickets/:ticketId`.

Set `PINECONE_API_KEY` in `.env` before using vector-backed retrieval, and
configure a chat provider key for the selected provider. The default Pinecone
indexes are `its-knowledge-base` for KB chunks, `its-tickets` for ticket
vectors, `its-comments` for ticket comment vectors, and `its-db-schema` for
database schema manifest retrieval. See `docs/architecture.md` for the current
schema, graph, guardrail, and RAG details.

## Import ticket CSV

To import historical tickets into SQLite and upsert each ticket into the Pinecone ticket index:

```bash
uv run python scripts/ingest_tickets.py --csv path/to/tickets.csv
```

For large CSVs, control database commits and vector upsert batch sizes:

```bash
uv run python scripts/ingest_tickets.py --csv path/to/tickets.csv \
  --db-batch-size 500 \
  --vector-batch-size 100
```

Supported CSV columns include the current export headers:

`ticket id`, `title`, `title clean`, `description`, `description clean`, `category`,
`component`, `issue type`, `severity`, `status`, `resolution`, `resolution clean`,
`error codes`, `quality score`, `embedding text`, `source`, `unified id`,
`import id`, `external record id`, `external source`, `language`, and `tags`.
When `embedding text` is present, ticket vector indexing uses that cleaned text.

Use `--dry-run` first to validate rows without writing data:

```bash
uv run python scripts/ingest_tickets.py --csv path/to/tickets.csv --dry-run
```

To rebuild the whole ticket vector index from database tickets later:

```bash
uv run python scripts/ingest_tickets.py
```

CSV import writes tickets even when Pinecone is unavailable. Set `PINECONE_API_KEY` and `PINECONE_TICKET_INDEX_NAME` when you want the same run to upsert vectors into the ticket index. Use `--skip-vector-index` to load SQLite first and index vectors later with `uv run python scripts/ingest_tickets.py`.

Model providers are centralized in `app/llm.py`.
- Chat: `LLM_PROVIDER=openai`, `LLM_PROVIDER=ollama`, `LLM_PROVIDER=nvidia`, or `LLM_PROVIDER=groq`.
- Embeddings: `EMBEDDING_PROVIDER=openai`, `EMBEDDING_PROVIDER=huggingface`, or `EMBEDDING_PROVIDER=nvidia`.

NVIDIA endpoint setup example:

```env
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=your-key
NVIDIA_MODEL=meta/llama-3.1-70b-instruct

EMBEDDING_PROVIDER=nvidia
NVIDIA_EMBEDDING_MODEL=nvidia/nv-embedqa-e5-v5
```

If Pinecone reports a vector dimension mismatch during ingestion, make the index dimension match the embedding output. For an existing 1024-dimensional Pinecone index with OpenAI embeddings, set `OPENAI_EMBEDDING_DIMENSIONS=1024` in `.env` and rerun ingestion. Otherwise recreate the Pinecone index with the default model dimension, such as 1536 for `text-embedding-3-small`.
