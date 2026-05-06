from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.comment_vector import (
    delete_comment_vector_entries,
    upsert_comment_vector_entry,
)
from app.db import (
    SessionLocal,
    claim_next_background_job,
    complete_background_job,
    fail_background_job,
    get_ticket,
)
from app.ticket_insights import refresh_ticket_insight
from app.ticket_vector import index_ticket

logger = logging.getLogger(__name__)


def run_pending_jobs(*, limit: int = 25) -> int:
    processed = 0
    for _ in range(max(limit, 1)):
        with SessionLocal() as db:
            job = claim_next_background_job(db)
            if job is None:
                return processed
            job_id = job.id
            kind = job.kind
            payload = dict(job.payload or {})

        try:
            _run_job(kind, payload)
        except Exception as exc:
            logger.exception("Background job %s failed", job_id)
            with SessionLocal() as db:
                existing = db.get(type(job), job_id)
                if existing is not None:
                    fail_background_job(db, existing, str(exc))
        else:
            with SessionLocal() as db:
                existing = db.get(type(job), job_id)
                if existing is not None:
                    complete_background_job(db, existing)
            processed += 1
    return processed


def run_worker_loop(
    *, poll_interval_seconds: float = 2.0, batch_size: int = 10
) -> None:
    while True:
        processed = run_pending_jobs(limit=batch_size)
        if processed == 0:
            time.sleep(max(poll_interval_seconds, 0.1))


def _run_job(kind: str, payload: dict[str, Any]) -> None:
    if kind == "ticket_vector_upsert":
        ticket_id = int(payload["ticket_id"])
        with SessionLocal() as db:
            ticket = get_ticket(db, ticket_id)
        if ticket is not None:
            index_ticket(ticket)
        return

    if kind == "comment_vector_upsert":
        asyncio.run(
            upsert_comment_vector_entry(
                int(payload["ticket_id"]),
                int(payload["comment_id"]),
            )
        )
        return

    if kind == "comment_vector_delete":
        asyncio.run(
            delete_comment_vector_entries(
                int(payload["ticket_id"]),
                [int(value) for value in payload.get("comment_ids", [])],
            )
        )
        return

    if kind == "ticket_insights_refresh":
        refresh_ticket_insight(int(payload["ticket_id"]))
        return

    raise ValueError(f"Unknown background job kind: {kind}")
