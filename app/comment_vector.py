"""Async vector index helpers for ticket comments."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.comment_llm import classify_comment_metadata
from app.db import SessionLocal, get_ticket, get_ticket_comment
from app.rag_ingest import get_vectorstore
from app.schemas import CommentMetadataFilters, CommentVectorMetadata
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommentVectorResult:
    ticket_id: int
    comment_id: int
    score: float
    summary: str
    metadata: dict[str, Any]
    content: str


class CommentVectorSearchUnavailable(RuntimeError):
    """Raised when the configured comment vector store cannot serve search."""


def get_comment_vectorstore(
    settings: Settings | None = None,
    *,
    persist_directory: str | Path | None = None,
    collection_name: str | None = None,
    index_name: str | None = None,
    embedding: Embeddings | None = None,
):
    active_settings = settings or get_settings()
    if settings is None and embedding is None and persist_directory is None:
        selected_index = (
            index_name or collection_name or active_settings.pinecone_comment_index_name
        )
        return _cached_comment_vectorstore(selected_index)
    return get_vectorstore(
        settings=active_settings,
        persist_directory=persist_directory,
        index_name=index_name
        or collection_name
        or active_settings.pinecone_comment_index_name,
        embedding=embedding,
    )


@lru_cache(maxsize=4)
def _cached_comment_vectorstore(index_name: str):
    return get_vectorstore(index_name=index_name)


async def upsert_comment_vector_entry(ticket_id: int, comment_id: int) -> bool:
    return await asyncio.to_thread(
        _upsert_comment_vector_entry_sync, ticket_id, comment_id
    )


async def delete_comment_vector_entries(ticket_id: int, comment_ids: list[int]) -> int:
    return await asyncio.to_thread(
        _delete_comment_vector_entries_sync, ticket_id, comment_ids
    )


def search_comment_vectors(
    query: str,
    *,
    metadata_filters: CommentMetadataFilters | None = None,
    project_id: int | None = None,
    project_ids: list[int] | None = None,
    user_clearance: str | None = None,
    limit: int = 10,
) -> list[CommentVectorResult]:
    clean_query = query.strip()
    if not clean_query:
        return []

    selected_limit = max(1, limit)
    search_k = max(selected_limit * 8, selected_limit)
    coarse_filter = _coarse_comment_filter(
        metadata_filters,
        project_id=project_id,
        project_ids=project_ids,
    )

    try:
        vectorstore = get_comment_vectorstore()
        raw_results = vectorstore.similarity_search_with_score(
            clean_query,
            k=search_k,
            filter=coarse_filter,
        )
    except Exception as exc:
        logger.warning("Comment vector search unavailable: %s", exc)
        raise CommentVectorSearchUnavailable(
            "Comment vector search unavailable"
        ) from exc

    results: list[CommentVectorResult] = []
    for document, distance in raw_results:
        metadata = dict(document.metadata or {})
        ticket_id = _parse_label_int(metadata.get("ticket_id"), prefix="T-")
        comment_id = _parse_label_int(metadata.get("comment_id"), prefix="C-")
        if ticket_id is None or comment_id is None:
            continue
        if not _metadata_matches_filters(
            metadata,
            metadata_filters,
            ticket_id=ticket_id,
            comment_id=comment_id,
            project_id=project_id,
            project_ids=project_ids,
            user_clearance=user_clearance,
        ):
            continue

        score = float(distance) if distance is not None else 0.0
        summary = str(metadata.get("summary") or "")
        results.append(
            CommentVectorResult(
                ticket_id=ticket_id,
                comment_id=comment_id,
                score=score,
                summary=summary,
                metadata=metadata,
                content=document.page_content,
            )
        )
        if len(results) >= selected_limit:
            break
    return results


def _upsert_comment_vector_entry_sync(ticket_id: int, comment_id: int) -> bool:
    try:
        with SessionLocal() as db:
            ticket = get_ticket(db, ticket_id)
            comment = get_ticket_comment(db, ticket_id=ticket_id, comment_id=comment_id)

        if ticket is None or comment is None:
            return False

        author = getattr(comment, "author", None)
        author_name = (
            getattr(author, "display_name", None)
            or getattr(author, "email", None)
            or f"User #{comment.author_user_id}"
        )
        ticket_description = str(
            ticket.raw_context.get("description") or ticket.intelligence.summary
        )

        classification = classify_comment_metadata(
            ticket_title=ticket.intelligence.summary,
            ticket_description=ticket_description,
            author=author_name,
            timestamp=comment.created_at,
            comment_text=comment.content,
        )

        metadata = CommentVectorMetadata(
            comment_id=_comment_label(comment.id),
            ticket_id=_ticket_label(ticket.id),
            tags=classification.tags,
            references_tickets=classification.references_tickets,
            references_systems=classification.references_systems,
            references_people=classification.references_people,
            signal_strength=classification.signal_strength,
            summary=classification.summary,
        ).model_dump(mode="json")

        metadata.update(
            {
                "comment_vector_id": _comment_vector_id(ticket.id, comment.id),
                "source_type": "ticket_comment",
                "author_user_id": comment.author_user_id,
                "parent_comment_id": comment.parent_comment_id or -1,
                "project_id": (
                    ticket.project_id if ticket.project_id is not None else -1
                ),
                "user_clearance": ticket.user_clearance.value,
                "clearance": ticket.user_clearance.value,
                "created_at": (
                    comment.created_at.isoformat() if comment.created_at else ""
                ),
                "updated_at": (
                    comment.updated_at.isoformat() if comment.updated_at else ""
                ),
            }
        )

        document = Document(
            page_content=_comment_embedding_content(
                ticket_title=ticket.intelligence.summary,
                ticket_description=ticket_description,
                author=author_name,
                comment_text=comment.content,
                metadata=metadata,
            ),
            metadata=metadata,
        )

        document_id = _comment_vector_id(ticket.id, comment.id)
        vectorstore = get_comment_vectorstore()
        try:
            vectorstore.delete(ids=[document_id])
        except Exception:
            logger.debug(
                "Comment vector id %s was not present before upsert", document_id
            )
        vectorstore.add_documents([document], ids=[document_id])
        return True
    except Exception as exc:
        logger.warning(
            "Unable to upsert comment vector for ticket %s comment %s: %s",
            ticket_id,
            comment_id,
            exc,
        )
        return False


def _delete_comment_vector_entries_sync(ticket_id: int, comment_ids: list[int]) -> int:
    unique_ids = sorted({comment_id for comment_id in comment_ids if comment_id > 0})
    if not unique_ids:
        return 0

    try:
        vectorstore = get_comment_vectorstore()
        vector_ids = [
            _comment_vector_id(ticket_id, comment_id) for comment_id in unique_ids
        ]
        vectorstore.delete(ids=vector_ids)
        return len(vector_ids)
    except Exception as exc:
        logger.warning(
            "Unable to delete comment vectors for ticket %s comments %s: %s",
            ticket_id,
            unique_ids,
            exc,
        )
        return 0


def _comment_vector_id(ticket_id: int, comment_id: int) -> str:
    return f"comment:{ticket_id}:{comment_id}"


def _comment_label(comment_id: int) -> str:
    return f"C-{comment_id}"


def _ticket_label(ticket_id: int) -> str:
    return f"T-{ticket_id}"


def _coarse_comment_filter(
    filters: CommentMetadataFilters | None,
    *,
    project_id: int | None = None,
    project_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    clauses: list[dict[str, Any]] = []
    if filters and filters.ticket_ids:
        labels = [_ticket_label(ticket_id) for ticket_id in filters.ticket_ids]
        if len(labels) == 1:
            clauses.append({"ticket_id": labels[0]})
        else:
            clauses.append({"ticket_id": {"$in": labels}})
    if filters and filters.comment_ids:
        labels = [_comment_label(comment_id) for comment_id in filters.comment_ids]
        if len(labels) == 1:
            clauses.append({"comment_id": labels[0]})
        else:
            clauses.append({"comment_id": {"$in": labels}})
    if project_id is not None:
        clauses.append({"project_id": project_id})
    elif project_ids:
        if len(project_ids) == 1:
            clauses.append({"project_id": project_ids[0]})
        else:
            clauses.append({"project_id": {"$in": project_ids}})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _metadata_matches_filters(
    metadata: dict[str, Any],
    filters: CommentMetadataFilters | None,
    *,
    ticket_id: int,
    comment_id: int,
    project_id: int | None = None,
    project_ids: list[int] | None = None,
    user_clearance: str | None = None,
) -> bool:
    if filters is None:
        return True

    if filters.ticket_ids and ticket_id not in set(filters.ticket_ids):
        return False
    if filters.comment_ids and comment_id not in set(filters.comment_ids):
        return False

    metadata_project_id = _int_metadata(metadata.get("project_id"))
    if project_id is not None and metadata_project_id != project_id:
        return False
    if project_id is None and project_ids:
        if metadata_project_id not in set(project_ids):
            return False

    if user_clearance:
        allowed = _allowed_clearance_values(user_clearance)
        metadata_clearance = str(
            metadata.get("user_clearance") or metadata.get("clearance") or ""
        ).casefold()
        if metadata_clearance and metadata_clearance not in allowed:
            return False

    if filters.tags:
        if not (set(filters.tags) & set(_metadata_list(metadata.get("tags")))):
            return False
    if filters.references_tickets:
        wanted = {value.casefold() for value in filters.references_tickets}
        found = {
            value.casefold()
            for value in _metadata_list(metadata.get("references_tickets"))
        }
        if not (wanted & found):
            return False
    if filters.references_systems:
        wanted = {value.casefold() for value in filters.references_systems}
        found = {
            value.casefold()
            for value in _metadata_list(metadata.get("references_systems"))
        }
        if not (wanted & found):
            return False
    if filters.references_people:
        wanted = {value.casefold() for value in filters.references_people}
        found = {
            value.casefold()
            for value in _metadata_list(metadata.get("references_people"))
        }
        if not (wanted & found):
            return False

    author_user_id = _int_metadata(metadata.get("author_user_id"))
    if filters.author_user_ids and author_user_id not in set(filters.author_user_ids):
        return False

    parent_comment_id = _int_metadata(metadata.get("parent_comment_id"))
    if filters.parent_comment_ids and parent_comment_id not in set(
        filters.parent_comment_ids
    ):
        return False

    signal_strength = _float_metadata(metadata.get("signal_strength"))
    if filters.min_signal_strength is not None and signal_strength < float(
        filters.min_signal_strength
    ):
        return False
    if filters.max_signal_strength is not None and signal_strength > float(
        filters.max_signal_strength
    ):
        return False
    return True


def _allowed_clearance_values(value: str) -> set[str]:
    normalized = str(value or "").casefold()
    values = {"public"}
    if normalized in {"internal", "restricted"}:
        values.add("internal")
    if normalized == "restricted":
        values.add("restricted")
    return values


def _metadata_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return []
        if "," in value:
            return [part.strip() for part in value.split(",") if part.strip()]
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _int_metadata(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_metadata(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_label_int(value: Any, *, prefix: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.upper().startswith(prefix.upper()):
        text = text[len(prefix) :]
    try:
        return int(text)
    except ValueError:
        return None


def _comment_embedding_content(
    *,
    ticket_title: str,
    ticket_description: str,
    author: str,
    comment_text: str,
    metadata: dict[str, Any],
) -> str:
    lines = [
        f"Ticket title: {ticket_title}",
        f"Ticket description: {ticket_description}",
        f"Comment author: {author}",
        f"Comment content: {comment_text}",
    ]

    summary = str(metadata.get("summary") or "").strip()
    if summary:
        lines.append(f"Comment summary: {summary}")

    tags = metadata.get("tags")
    if isinstance(tags, list) and tags:
        lines.append(f"Tags: {', '.join(str(tag) for tag in tags)}")

    references_tickets = metadata.get("references_tickets")
    if isinstance(references_tickets, list) and references_tickets:
        lines.append(
            "Referenced tickets: "
            + ", ".join(str(ticket_id) for ticket_id in references_tickets)
        )

    references_systems = metadata.get("references_systems")
    if isinstance(references_systems, list) and references_systems:
        lines.append(
            "Referenced systems: "
            + ", ".join(str(system_name) for system_name in references_systems)
        )

    references_people = metadata.get("references_people")
    if isinstance(references_people, list) and references_people:
        lines.append(
            "Referenced people: "
            + ", ".join(str(person) for person in references_people)
        )

    lines.append(
        f"Signal strength: {float(metadata.get('signal_strength') or 0.0):.2f}"
    )
    return "\n".join(lines)
