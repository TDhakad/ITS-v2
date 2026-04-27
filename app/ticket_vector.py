"""Pinecone indexing and semantic search helpers for helpdesk tickets."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.rag_ingest import (
    _coerce_clearance_level,
    _normalize_many,
    _normalize_term,
    _sanitize_metadata,
    get_vectorstore,
)
from app.schemas import ChatMessage, KBArticleRef, TicketRead
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_TICKET_INDEX_NAME = "its-tickets"
DEFAULT_TICKET_COLLECTION_NAME = DEFAULT_TICKET_INDEX_NAME
MAX_INDEXED_MESSAGES = 12
MAX_MESSAGE_CHARS = 1200


@dataclass(frozen=True)
class TicketVectorResult:
    ticket_id: int
    score: float
    summary: str
    metadata: dict[str, Any]
    content: str


class TicketVectorSearchUnavailable(RuntimeError):
    """Raised when the configured ticket vector store cannot serve search."""


def get_ticket_vectorstore(
    settings: Settings | None = None,
    *,
    persist_directory: str | Path | None = None,
    collection_name: str | None = None,
    index_name: str | None = None,
    embedding: Embeddings | None = None,
):
    active_settings = settings or get_settings()
    return get_vectorstore(
        settings=active_settings,
        persist_directory=persist_directory,
        index_name=index_name or collection_name or active_settings.pinecone_ticket_index_name,
        embedding=embedding,
    )


def ticket_to_document(ticket: TicketRead) -> Document:
    metadata = _ticket_metadata(ticket)
    return Document(page_content=_ticket_content(ticket), metadata=metadata)


def index_ticket(ticket: TicketRead) -> bool:
    """Upsert one ticket into Pinecone.

    The SQL ticket remains the source of truth. This helper is intentionally small
    and returns False on index failures so ticket creation does not roll back due
    to an embedding/vector-store outage.
    """

    try:
        document = ticket_to_document(ticket)
        document_id = str(document.metadata["ticket_vector_id"])
        vectorstore = get_ticket_vectorstore()
        try:
            vectorstore.delete(ids=[document_id])
        except Exception:
            logger.debug("Ticket vector id %s was not present before upsert", document_id)
        vectorstore.add_documents([document], ids=[document_id])
        return True
    except Exception as exc:
        logger.warning("Unable to index ticket %s in Pinecone: %s", ticket.id, exc)
        return False


def index_tickets(
    tickets: Iterable[TicketRead],
    *,
    reset: bool = False,
    batch_size: int = 100,
) -> int:
    vectorstore = get_ticket_vectorstore()
    if reset:
        try:
            vectorstore.delete(delete_all=True)
        except Exception:
            logger.debug("Ticket vector index was empty before reset")
        vectorstore = get_ticket_vectorstore()

    total = 0
    documents: list[Document] = []
    selected_batch_size = max(batch_size, 1)
    for ticket in tickets:
        documents.append(ticket_to_document(ticket))
        if len(documents) >= selected_batch_size:
            total += _add_ticket_documents(vectorstore, documents)
            documents = []

    if documents:
        total += _add_ticket_documents(vectorstore, documents)
    return total


def _add_ticket_documents(vectorstore, documents: list[Document]) -> int:
    vectorstore.add_documents(
        documents,
        ids=[str(document.metadata["ticket_vector_id"]) for document in documents],
    )
    return len(documents)


def search_ticket_vectors(
    query: str,
    *,
    user_id: str | None = None,
    project_id: int | None = None,
    project_ids: list[int] | None = None,
    tag_slugs: list[str] | None = None,
    status: str | None = None,
    priority: str | None = None,
    exclude_ticket_id: int | None = None,
    limit: int = 10,
) -> list[TicketVectorResult]:
    clean_query = query.strip()
    if not clean_query:
        return []

    try:
        vectorstore = get_ticket_vectorstore()
        raw_results = vectorstore.similarity_search_with_score(
            clean_query,
            k=max(limit * 4, limit),
            filter=_ticket_filter(
                user_id=user_id,
                project_id=project_id,
                project_ids=project_ids,
                status=status,
                priority=priority,
            ),
        )
    except Exception as exc:
        logger.warning("Ticket vector search unavailable: %s", exc)
        raise TicketVectorSearchUnavailable("Ticket vector search unavailable") from exc

    wanted_tags = set(_normalize_many(tag_slugs))
    results: list[TicketVectorResult] = []
    for document, distance in raw_results:
        metadata = dict(document.metadata or {})
        ticket_id = _int_metadata(metadata.get("ticket_id"))
        if ticket_id is None or ticket_id == exclude_ticket_id:
            continue
        if wanted_tags and not (wanted_tags & set(_normalize_many(metadata.get("tags")))):
            continue
        score = float(distance) if distance is not None else 0.0
        results.append(
            TicketVectorResult(
                ticket_id=ticket_id,
                score=score,
                summary=str(metadata.get("title") or f"Ticket #{ticket_id}"),
                metadata=metadata,
                content=document.page_content,
            )
        )
        if len(results) >= limit:
            break
    return results


def ticket_vector_result_to_api(result: TicketVectorResult) -> dict[str, Any]:
    metadata = result.metadata
    project_id = _int_metadata(metadata.get("project_id"))
    if project_id is not None and project_id < 0:
        project_id = None
    return {
        "ticket_id": result.ticket_id,
        "summary": result.summary,
        "status": str(metadata.get("status") or ""),
        "priority": str(metadata.get("priority") or ""),
        "category": str(metadata.get("category") or ""),
        "user_id": str(metadata.get("user_id") or ""),
        "project_id": project_id,
        "tags": _normalize_many(metadata.get("tags")),
        "created_at": str(metadata.get("created_at") or ""),
        "score": result.score,
        "source": "vector",
    }


def _ticket_metadata(ticket: TicketRead) -> dict[str, str | int | float | bool]:
    keywords = ", ".join(ticket.intelligence.keywords)
    kb_ids = ", ".join(article.kb_id for article in ticket.resolution.linked_kb_articles)
    duplicate_ids = ", ".join(
        str(ticket_id) for ticket_id in ticket.resolution.duplicate_ticket_ids
    )
    clearance_level = _coerce_clearance_level(ticket.user_clearance)
    tags = ", ".join(_normalize_many(ticket.tag_slugs))
    metadata = {
        "source": f"ticket:{ticket.id}",
        "source_type": "ticket",
        "ticket_vector_id": f"ticket:{ticket.id}",
        "ticket_id": ticket.id,
        "title": ticket.intelligence.summary[:300],
        "status": ticket.status.value,
        "status_norm": _normalize_term(ticket.status),
        "user_id": ticket.user_id,
        "thread_id": ticket.thread_id,
        "app_name": _normalize_term(ticket.app_name or "general"),
        "environment": _normalize_term(ticket.environment),
        "user_clearance": ticket.user_clearance.value,
        "clearance": ticket.user_clearance.value,
        "clearance_level": clearance_level,
        "category": ticket.intelligence.category.value,
        "category_norm": _normalize_term(ticket.intelligence.category),
        "priority": ticket.intelligence.suggested_priority.value,
        "priority_norm": _normalize_term(ticket.intelligence.suggested_priority),
        "project_id": ticket.project_id if ticket.project_id is not None else -1,
        "tags": tags,
        "keywords": keywords,
        "confidence": ticket.intelligence.confidence,
        "linked_kb_ids": kb_ids,
        "duplicate_ticket_ids": duplicate_ids,
        "conversation_turns": len(ticket.conversation),
        "has_guardrail": ticket.guardrail is not None,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else "",
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else "",
    }
    return _sanitize_metadata(metadata)


def _ticket_content(ticket: TicketRead) -> str:
    parts = [
        f"Ticket #{ticket.id}",
        f"Summary: {ticket.intelligence.summary}",
        (
            "Classification: "
            f"{ticket.intelligence.category.value}, "
            f"{ticket.intelligence.suggested_priority.value} priority"
        ),
    ]
    if ticket.app_name:
        parts.append(f"Application: {ticket.app_name}")
    if ticket.environment:
        parts.append(f"Environment: {ticket.environment.value}")
    if ticket.intelligence.keywords:
        parts.append(f"Keywords: {', '.join(ticket.intelligence.keywords)}")
    if ticket.tag_slugs:
        parts.append(f"Tags: {', '.join(ticket.tag_slugs)}")
    if ticket.resolution.linked_kb_articles:
        parts.append(f"Linked KB: {_linked_kb_text(ticket.resolution.linked_kb_articles)}")
    if ticket.resolution.suggested_fixes:
        parts.append(f"Suggested fixes: {'; '.join(ticket.resolution.suggested_fixes)}")
    if ticket.raw_context:
        embedding_text = ticket.raw_context.get("embedding_text")
        if embedding_text:
            parts.append(f"Search text: {_truncate(str(embedding_text), MAX_MESSAGE_CHARS)}")
            return "\n".join(part for part in parts if part)
    if ticket.conversation:
        parts.append("Conversation:")
        parts.extend(_message_lines(ticket.conversation))
    if ticket.raw_context:
        description = ticket.raw_context.get("description")
        if description:
            parts.append(f"Original description: {_truncate(str(description), MAX_MESSAGE_CHARS)}")
    return "\n".join(part for part in parts if part)


def _ticket_filter(
    *,
    user_id: str | None,
    project_id: int | None,
    project_ids: list[int] | None = None,
    status: str | None,
    priority: str | None,
) -> dict[str, Any] | None:
    clauses: list[dict[str, Any]] = []
    if user_id:
        clauses.append({"user_id": user_id})
    # project_id (single) takes precedence; fall back to $in for multiple projects.
    if project_id is not None:
        clauses.append({"project_id": project_id})
    elif project_ids:
        if len(project_ids) == 1:
            clauses.append({"project_id": project_ids[0]})
        else:
            clauses.append({"project_id": {"$in": project_ids}})
    if status:
        clauses.append({"status": status})
    if priority:
        clauses.append({"priority": priority})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _linked_kb_text(articles: list[KBArticleRef]) -> str:
    return "; ".join(f"{article.title} ({article.kb_id})" for article in articles)


def _message_lines(messages: list[ChatMessage]) -> list[str]:
    lines: list[str] = []
    for message in messages[:MAX_INDEXED_MESSAGES]:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)
        lines.append(f"{role}: {_truncate(message.content, MAX_MESSAGE_CHARS)}")
    return lines


def _truncate(value: str, max_chars: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_chars:
        return clean
    return f"{clean[:max_chars].rstrip()}..."


def _int_metadata(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
