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

Open `http://127.0.0.1:8000` for the chatbot and `http://127.0.0.1:8000/admin` for the admin dashboard.

Set `OPENAI_API_KEY` and `PINECONE_API_KEY` in `.env` before using the LangGraph chatbot. The default Pinecone indexes are `its-knowledge-base` and `its-tickets`. See `docs/architecture.md` for the schema, graph, guardrail, and RAG details.

Model providers are centralized in `app/llm.py`. Switch chat to Ollama with `LLM_PROVIDER=ollama`; switch embeddings with `EMBEDDING_PROVIDER=openai` or `EMBEDDING_PROVIDER=huggingface`.
