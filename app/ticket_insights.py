from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from app.db import (
    SessionLocal,
    find_recent_similar_tickets,
    get_kb_project_ids,
    get_ticket,
    save_ticket_insight,
)
from app.llm import get_chat_model
from app.rag import HybridRAGPipeline, context_from_user
from app.schemas import KBArticleRef, TicketRead, UserClearance

logger = logging.getLogger(__name__)

INSIGHT_CACHE_TTL_SECONDS = 900


def ticket_insight_cache_key(ticket: TicketRead) -> str:
    updated_at = ticket.updated_at.isoformat() if ticket.updated_at else ""
    return f"{ticket.id}:{ticket.status.value}:{updated_at}"


def build_ticket_insights_payload(ticket: TicketRead) -> dict[str, Any]:
    similar_tickets = find_recent_similar_tickets(
        ticket.intelligence.summary,
        exclude_ticket_id=ticket.id,
        project_id=ticket.project_id,
        limit=20,
    )
    duplicates = sorted(
        similar_tickets,
        key=lambda item: float(item.get("score") or 0.0),
        reverse=True,
    )[:10]
    recent_related = similar_tickets[:10]

    kb_refs: list[KBArticleRef] = []
    retrieval_available = True
    docs: list[Document] = []
    try:
        rag = HybridRAGPipeline()
        context = context_from_user(
            category=ticket.intelligence.category,
            app_name=ticket.app_name,
            environment=ticket.environment,
            clearance=UserClearance.INTERNAL,
        )
        docs = _filter_docs_for_project(
            rag.retrieve(ticket.intelligence.summary, context, k=5),
            project_id=ticket.project_id,
        )
        kb_refs = rag.article_refs(docs)
    except Exception:
        logger.exception("Ticket insight knowledge retrieval failed")
        retrieval_available = False

    recommended_action, suggested_actions = _generate_recommended_actions(
        ticket,
        kb_refs,
        duplicates,
        recent_related,
    )

    citations = [_kb_ref_to_api(ref) for ref in kb_refs]
    return {
        "ticket_id": ticket.id,
        "summary": ticket.intelligence.summary,
        "recommended_action": recommended_action,
        "suggested_priority": ticket.intelligence.suggested_priority.value,
        "signals": [
            ticket.intelligence.category.value,
            *ticket.intelligence.keywords[:6],
            *(f"duplicate #{duplicate['ticket_id']}" for duplicate in duplicates[:3]),
        ],
        "citations": citations,
        "references": citations,
        "duplicates": duplicates,
        "recent_tickets": [
            {
                "ticket_id": item.get("ticket_id"),
                "summary": item.get("summary"),
                "status": item.get("status"),
                "priority": item.get("priority"),
                "created_at": item.get("created_at"),
            }
            for item in recent_related
        ],
        "suggested_fixes": suggested_actions,
        "retrieved_chunks": len(docs),
        "retrieval_available": retrieval_available,
    }


def refresh_ticket_insight(ticket_id: int) -> dict[str, Any] | None:
    with SessionLocal() as db:
        ticket = get_ticket(db, ticket_id)
        if ticket is None:
            return None
        payload = build_ticket_insights_payload(ticket)
        save_ticket_insight(
            db,
            ticket_id=ticket.id,
            cache_key=ticket_insight_cache_key(ticket),
            payload=payload,
            ttl_seconds=INSIGHT_CACHE_TTL_SECONDS,
        )
        return payload


def _filter_docs_for_project(
    docs: list[Document],
    *,
    project_id: int | None,
) -> list[Document]:
    if project_id is None:
        return docs

    allowed: list[Document] = []
    with SessionLocal() as db:
        for doc in docs:
            linked_project_ids: set[int] = set()
            for identifier in _kb_identifiers(doc.metadata):
                linked_project_ids.update(get_kb_project_ids(db, identifier))
            if not linked_project_ids or project_id in linked_project_ids:
                allowed.append(doc)
    return allowed


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


def _kb_ref_to_api(ref: KBArticleRef) -> dict[str, Any]:
    return {
        "kb_id": ref.kb_id,
        "title": ref.title,
        "source": ref.source,
        "score": ref.relevance_score,
        "relevance_score": ref.relevance_score,
        "clearance": ref.clearance.value,
        "summary": f"{ref.title} ({ref.clearance.value})",
    }


def _generate_recommended_actions(
    ticket: TicketRead,
    refs: list[KBArticleRef],
    duplicates: list[dict[str, Any]],
    recent_tickets: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    payload = {
        "ticket": {
            "id": ticket.id,
            "summary": ticket.intelligence.summary,
            "category": ticket.intelligence.category.value,
            "priority": ticket.intelligence.suggested_priority.value,
            "keywords": ticket.intelligence.keywords[:8],
            "app_name": ticket.app_name,
            "environment": ticket.environment.value,
        },
        "similar_tickets_by_score": duplicates[:6],
        "recent_similar_tickets": recent_tickets[:10],
        "knowledge_refs": [
            {
                "kb_id": ref.kb_id,
                "title": ref.title,
                "source": ref.source,
                "relevance_score": ref.relevance_score,
            }
            for ref in refs[:6]
        ],
    }

    try:
        response = get_chat_model().invoke(
            [
                SystemMessage(
                    content=(
                        "You are an IT operations copilot generating incident recommendations. "
                        "Use only provided evidence. Return exactly 1 to 4 concise "
                        "recommended actions, one per line. No bullets, numbering, or markdown."
                    )
                ),
                HumanMessage(content=json.dumps(payload, ensure_ascii=True)),
            ]
        )
        actions = _extract_action_lines(
            _coerce_llm_text(getattr(response, "content", response))
        )
        if actions:
            return actions[0], actions
    except Exception:
        logger.exception("Ticket insight recommendation generation failed")

    fallback = (
        "Review retrieved similar tickets and knowledge references before deciding "
        "next workflow step."
    )
    return fallback, [fallback]


def _coerce_llm_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(value).strip()


def _extract_action_lines(text: str, *, max_items: int = 4) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        candidate = line.strip().lstrip("-*0123456789. ").strip()
        if not candidate:
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        cleaned.append(candidate)
        seen.add(key)
        if len(cleaned) >= max_items:
            break
    return cleaned or ([text.strip()] if text.strip() else [])
