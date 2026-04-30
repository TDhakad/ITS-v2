"""Import CSV tickets into SQLite and index tickets into Pinecone."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import (SessionLocal, TagRecord, create_tickets,  # noqa: E402
                    init_db, list_tickets)
from app.schemas import (ChatMessage, Environment, Priority,  # noqa: E402
                         ResolutionData, TicketCategory, TicketCreate,
                         TicketIntelligence, TicketStatus, UserClearance)
from app.ticket_vector import (DEFAULT_TICKET_INDEX_NAME,  # noqa: E402
                               index_tickets)

MAX_SUMMARY_CHARS = 1_200
MAX_MESSAGE_CHARS = 8_000
DEFAULT_IMPORT_USER = "csv-import"
DEFAULT_CONFIDENCE = 0.75

COLUMN_ALIASES = {
    "summary": ("summary", "title_clean", "title", "subject", "issue", "problem"),
    "description": (
        "description_clean",
        "description",
        "embedding_text",
        "details",
        "body",
        "message",
        "request",
        "issue",
        "problem",
    ),
    "user_id": ("user_id", "requester", "requester_id", "created_by", "email", "user"),
    "thread_id": (
        "thread_id",
        "conversation_id",
        "session_id",
        "ticket_id",
        "ticket_number",
        "unified_id",
        "external_record_id",
        "import_id",
        "id",
    ),
    "status": ("status", "state"),
    "category": ("category", "issue_type", "type"),
    "priority": ("priority", "severity"),
    "app_name": ("app_name", "component", "application", "app", "system", "service"),
    "environment": ("environment", "env"),
    "keywords": ("keywords", "keyword", "error_codes", "error_code"),
    "tags": ("tags", "tag_slugs", "tag"),
    "confidence": ("confidence", "quality_score", "quality"),
    "project_id": ("project_id",),
    "user_clearance": ("user_clearance", "clearance"),
    "conversation": ("conversation", "messages", "chat_history"),
    "assistant_reply": (
        "assistant_reply",
        "assistant_response",
        "response",
        "resolution_clean",
        "resolution",
    ),
    "suggested_fixes": (
        "suggested_fixes",
        "fixes",
        "resolution_steps",
        "resolution_clean",
    ),
    "duplicate_ticket_ids": (
        "duplicate_ticket_ids",
        "duplicates",
        "related_ticket_ids",
    ),
    "created_at": ("created_at", "created", "opened_at", "opened"),
    "updated_at": ("updated_at", "updated", "last_updated", "modified_at"),
    "embedding_text": ("embedding_text",),
    "issue_type": ("issue_type",),
    "error_codes": ("error_codes", "error_code"),
}

STOPWORDS = {
    "about",
    "access",
    "after",
    "again",
    "and",
    "are",
    "cannot",
    "for",
    "from",
    "have",
    "help",
    "into",
    "need",
    "not",
    "request",
    "the",
    "this",
    "ticket",
    "with",
}


@dataclass
class ImportReport:
    csv_path: Path
    created_ticket_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    index_errors: list[str] = field(default_factory=list)
    dry_run_rows: int = 0
    indexed: int = 0

    @property
    def created(self) -> int:
        return len(self.created_ticket_ids)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        help=(
            "Import ticket rows from a CSV file. Supports the export columns "
            "ticket id, title clean, description clean, component, issue type, "
            "severity, status, resolution clean, error codes, and embedding text."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of tickets to import. Omit (or pass 0) to import all.",
    )
    parser.add_argument(
        "--start-record",
        type=int,
        default=1,
        help="First data record to import, counting the first CSV data row as 1.",
    )
    parser.add_argument(
        "--end-record",
        type=int,
        help="Last data record to import, inclusive. Omit to import through the file end.",
    )
    parser.add_argument(
        "--db-batch-size",
        type=int,
        default=500,
        help="Number of CSV tickets to insert per database commit.",
    )
    parser.add_argument(
        "--vector-batch-size",
        type=int,
        default=100,
        help="Number of imported tickets to send per vector-store embedding/upsert batch.",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Append/upsert into the existing ticket index instead of clearing it first.",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="After CSV import, rebuild/upsert the ticket vector index from database tickets.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate CSV rows without writing to the database or Pinecone.",
    )
    parser.add_argument(
        "--skip-vector-index",
        action="store_true",
        help="Write CSV rows to SQLite only; skip ticket vector indexing.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep importing valid rows when one CSV row is invalid.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db()

    if args.csv:
        report = import_csv_tickets(
            args.csv,
            dry_run=args.dry_run,
            continue_on_error=args.continue_on_error,
            start_record=args.start_record,
            end_record=args.end_record,
            db_batch_size=args.db_batch_size,
            vector_batch_size=args.vector_batch_size,
            index_vectors=not args.skip_vector_index,
        )
        print_import_report(report)
        if args.dry_run or not args.reindex:
            return

    count = reindex_ticket_vectors(
        limit=args.limit,
        reset=not args.no_reset,
        batch_size=args.vector_batch_size,
    )
    print(
        "\n".join(
            [
                "Tickets indexed.",
                f"index_name: {DEFAULT_TICKET_INDEX_NAME}",
                f"tickets: {count}",
            ]
        )
    )


def import_csv_tickets(
    csv_path: Path,
    *,
    dry_run: bool = False,
    continue_on_error: bool = False,
    start_record: int = 1,
    end_record: int | None = None,
    db_batch_size: int = 500,
    vector_batch_size: int = 100,
    index_vectors: bool = True,
) -> ImportReport:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    if start_record < 1:
        raise ValueError("--start-record must be 1 or greater")
    if end_record is not None and end_record < start_record:
        raise ValueError("--end-record must be greater than or equal to --start-record")

    report = ImportReport(csv_path=csv_path)
    pending: list[TicketCreate] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError("CSV file must contain a header row.")

        with SessionLocal() as db:
            for record_number, row in enumerate(reader, start=1):
                if record_number < start_record:
                    continue
                if end_record is not None and record_number > end_record:
                    break

                row_number = record_number + 1
                persisting_batch = False
                try:
                    ticket = ticket_from_csv_row(row, row_number=row_number)
                    if dry_run:
                        report.dry_run_rows += 1
                        continue

                    pending.append(ticket)
                    if len(pending) >= max(db_batch_size, 1):
                        persisting_batch = True
                        persist_ticket_batch(
                            db,
                            pending,
                            report,
                            index_vectors=index_vectors,
                            vector_batch_size=vector_batch_size,
                        )
                        pending = []
                except Exception as exc:
                    message = f"row {row_number}: {exc}"
                    report.errors.append(message)
                    if persisting_batch:
                        pending = []
                    if not continue_on_error:
                        raise ValueError(message) from exc
            if pending:
                persist_ticket_batch(
                    db,
                    pending,
                    report,
                    index_vectors=index_vectors,
                    vector_batch_size=vector_batch_size,
                )
    return report


def persist_ticket_batch(
    db,
    tickets: Sequence[TicketCreate],
    report: ImportReport,
    *,
    index_vectors: bool,
    vector_batch_size: int,
) -> None:
    try:
        ensure_tag_slugs(db, [slug for ticket in tickets for slug in ticket.tag_slugs])
        created = create_tickets(db, tickets, index_vectors=False)
    except Exception:
        db.rollback()
        raise
    report.created_ticket_ids.extend(ticket.id for ticket in created)
    if not index_vectors:
        return
    try:
        report.indexed += index_tickets(
            created, reset=False, batch_size=vector_batch_size
        )
    except Exception as exc:
        report.index_errors.append(str(exc))


def reindex_ticket_vectors(*, limit: int, reset: bool, batch_size: int) -> int:
    with SessionLocal() as db:
        tickets = list_tickets(db, None, limit=limit)
    return index_tickets(tickets, reset=reset, batch_size=batch_size)


def ticket_from_csv_row(row: Mapping[str, Any], *, row_number: int) -> TicketCreate:
    values = normalize_row(row)
    created_at = parse_datetime(first_value(values, "created_at"))
    updated_at = parse_datetime(first_value(values, "updated_at")) or created_at

    embedding_text = clean_text(first_value(values, "embedding_text"))
    description = clean_text(first_value(values, "description"))
    vector_description = embedding_text or description
    summary = clean_text(first_value(values, "summary")) or first_sentence(
        vector_description
    )
    if not summary:
        raise ValueError("missing summary/title/description")

    app_name = clean_text(first_value(values, "app_name")) or None
    issue_type = clean_text(first_value(values, "issue_type"))
    category_text = first_value(values, "category")
    keywords = parse_list(first_value(values, "keywords"))
    keywords.extend(parse_list(first_value(values, "error_codes")))
    keywords.extend(value for value in [app_name, issue_type] if value)
    if not keywords:
        keywords = infer_keywords(
            " ".join([summary, vector_description, category_text])
        )

    tags = [slugify(value) for value in parse_list(first_value(values, "tags"))]
    if not tags:
        tags = [
            slugify(value)
            for value in [category_text, issue_type, app_name]
            if clean_text(value)
        ]
    tags = [tag for tag in tags if tag]

    conversation = parse_conversation(first_value(values, "conversation"))
    if not conversation:
        conversation = [
            ChatMessage(
                role="user",
                content=truncate(
                    description or vector_description or summary, MAX_MESSAGE_CHARS
                ),
                created_at=created_at or datetime.now(UTC),
            )
        ]
    assistant_reply = clean_text(first_value(values, "assistant_reply"))
    if assistant_reply:
        conversation.append(
            ChatMessage(
                role="assistant",
                content=truncate(assistant_reply, MAX_MESSAGE_CHARS),
                created_at=updated_at or datetime.now(UTC),
            )
        )

    return TicketCreate(
        user_id=clean_text(first_value(values, "user_id")) or DEFAULT_IMPORT_USER,
        thread_id=clean_text(first_value(values, "thread_id"))
        or f"csv-row-{row_number}",
        status=parse_status(first_value(values, "status")),
        app_name=app_name,
        environment=parse_environment(first_value(values, "environment")),
        user_clearance=parse_clearance(first_value(values, "user_clearance")),
        project_id=parse_int(first_value(values, "project_id")),
        tag_slugs=dedupe(tags)[:10],
        intelligence=TicketIntelligence(
            category=parse_category(category_text),
            suggested_priority=parse_priority(first_value(values, "priority")),
            summary=truncate(summary, MAX_SUMMARY_CHARS),
            keywords=dedupe(keywords)[:12],
            confidence=parse_confidence(first_value(values, "confidence")),
        ),
        resolution=ResolutionData(
            duplicate_ticket_ids=parse_int_list(
                first_value(values, "duplicate_ticket_ids")
            ),
            suggested_fixes=parse_list(first_value(values, "suggested_fixes"))[:10],
        ),
        conversation=conversation[:200],
        raw_context={
            "import_source": "csv",
            "csv_row": row_number,
            "description": description,
            "embedding_text": embedding_text,
            "issue_type": issue_type,
            "error_codes": parse_list(first_value(values, "error_codes")),
            "csv": {str(key): value for key, value in row.items()},
        },
        created_at=created_at,
        updated_at=updated_at,
    )


def print_import_report(report: ImportReport) -> None:
    lines = [
        "CSV tickets processed.",
        f"csv_path: {report.csv_path}",
        f"created: {report.created}",
        f"indexed: {report.indexed}",
        f"dry_run_rows: {report.dry_run_rows}",
        f"errors: {len(report.errors)}",
        f"index_errors: {len(report.index_errors)}",
    ]
    if report.created_ticket_ids:
        shown_ids = ", ".join(
            str(ticket_id) for ticket_id in report.created_ticket_ids[:20]
        )
        lines.append(f"ticket_ids: {shown_ids}")
    if report.errors:
        lines.extend(report.errors[:10])
    if report.index_errors:
        lines.extend(f"index error: {error}" for error in report.index_errors[:3])
    print("\n".join(lines))


def ensure_tag_slugs(db, slugs: Sequence[str]) -> None:
    wanted = [slug for slug in dedupe(slugs) if slug]
    if not wanted:
        return
    existing = set(
        db.scalars(select(TagRecord.slug).where(TagRecord.slug.in_(wanted))).all()
    )
    for slug in wanted:
        if slug not in existing:
            db.add(TagRecord(name=slug.replace("-", " ").title(), slug=slug))
    db.flush()


def normalize_row(row: Mapping[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        normalized[normalize_key(str(key))] = (
            "" if value is None else str(value).strip()
        )
    return normalized


def first_value(row: Mapping[str, str], canonical_name: str) -> str:
    for alias in COLUMN_ALIASES[canonical_name]:
        value = row.get(normalize_key(alias))
        if value:
            return value
    return ""


def parse_status(value: str) -> TicketStatus:
    return parse_enum(
        TicketStatus,
        value,
        TicketStatus.OPEN,
        {
            "new": TicketStatus.OPEN,
            "todo": TicketStatus.OPEN,
            "triage": TicketStatus.TRIAGED,
            "inprogress": TicketStatus.IN_PROGRESS,
            "progress": TicketStatus.IN_PROGRESS,
            "done": TicketStatus.RESOLVED,
            "complete": TicketStatus.RESOLVED,
        },
    )


def parse_category(value: str) -> TicketCategory:
    if not value:
        return TicketCategory.INFRA

    try:
        return parse_enum(
            TicketCategory,
            value,
            TicketCategory.INFRA,
            {
                "infrastructure": TicketCategory.INFRA,
                "network": TicketCategory.INFRA,
                "access": TicketCategory.INFRA,
                "software": TicketCategory.BUG,
                "defect": TicketCategory.BUG,
                "frontend": TicketCategory.UI,
                "feature-request": TicketCategory.FEATURE,
                "enhancement": TicketCategory.FEATURE,
                "device": TicketCategory.HARDWARE,
            },
        )
    except ValueError:
        text = value.casefold()
        if any(term in text for term in ("ui", "ux", "frontend", "screen", "render")):
            return TicketCategory.UI
        if any(term in text for term in ("hardware", "laptop", "device", "printer")):
            return TicketCategory.HARDWARE
        if any(term in text for term in ("bug", "defect", "error", "fail", "incident")):
            return TicketCategory.BUG
        if any(term in text for term in ("feature", "enhancement", "request")):
            return TicketCategory.FEATURE
        return TicketCategory.INFRA


def parse_priority(value: str) -> Priority:
    return parse_enum(
        Priority,
        value,
        Priority.MEDIUM,
        {
            "p0": Priority.CRITICAL,
            "1": Priority.CRITICAL,
            "p1": Priority.CRITICAL,
            "urgent": Priority.CRITICAL,
            "2": Priority.HIGH,
            "p2": Priority.HIGH,
            "major": Priority.HIGH,
            "3": Priority.MEDIUM,
            "p3": Priority.MEDIUM,
            "med": Priority.MEDIUM,
            "normal": Priority.MEDIUM,
            "4": Priority.LOW,
            "p4": Priority.LOW,
            "minor": Priority.LOW,
        },
    )


def parse_environment(value: str) -> Environment:
    return parse_enum(
        Environment,
        value,
        Environment.UNKNOWN,
        {
            "prod": Environment.PRODUCTION,
            "production": Environment.PRODUCTION,
            "stage": Environment.STAGING,
            "stg": Environment.STAGING,
            "dev": Environment.DEVELOPMENT,
            "development": Environment.DEVELOPMENT,
            "local": Environment.DEVELOPMENT,
        },
    )


def parse_clearance(value: str) -> UserClearance:
    return parse_enum(
        UserClearance,
        value,
        UserClearance.PUBLIC,
        {
            "employee": UserClearance.PUBLIC,
            "helpdesk": UserClearance.INTERNAL,
            "support": UserClearance.INTERNAL,
            "private": UserClearance.RESTRICTED,
        },
    )


def parse_enum(enum_type, value: str, default, aliases: Mapping[str, Any]):
    if not value:
        return default
    normalized = compact(value)
    normalized_aliases = {
        compact(alias): enum_value for alias, enum_value in aliases.items()
    }
    if normalized in normalized_aliases:
        return normalized_aliases[normalized]
    for member in enum_type:
        if normalized in {compact(member.name), compact(member.value)}:
            return member
    for part in re.split(r"[/,|>]", value):
        if part.strip() and part.strip() != value.strip():
            try:
                return parse_enum(enum_type, part, default, aliases)
            except ValueError:
                pass
    allowed = ", ".join(member.value for member in enum_type)
    raise ValueError(
        f"invalid {enum_type.__name__} {value!r}; expected one of: {allowed}"
    )


def parse_conversation(value: str) -> list[ChatMessage]:
    if not value:
        return []
    parsed = parse_json(value)
    if parsed is None:
        return [ChatMessage(role="user", content=truncate(value, MAX_MESSAGE_CHARS))]
    if isinstance(parsed, str):
        return [ChatMessage(role="user", content=truncate(parsed, MAX_MESSAGE_CHARS))]
    if isinstance(parsed, Mapping):
        parsed = [parsed]
    if not isinstance(parsed, Sequence):
        return []

    messages: list[ChatMessage] = []
    for item in parsed:
        if isinstance(item, str):
            messages.append(
                ChatMessage(role="user", content=truncate(item, MAX_MESSAGE_CHARS))
            )
            continue
        if not isinstance(item, Mapping):
            continue
        content = clean_text(
            item.get("content") or item.get("message") or item.get("text")
        )
        if not content:
            continue
        messages.append(
            ChatMessage(
                role=clean_text(item.get("role")) or "user",
                content=truncate(content, MAX_MESSAGE_CHARS),
                created_at=parse_datetime(clean_text(item.get("created_at")))
                or datetime.now(UTC),
            )
        )
    return messages


def parse_list(value: str) -> list[str]:
    if not value:
        return []
    parsed = parse_json(value)
    if isinstance(parsed, list):
        return [clean_text(item) for item in parsed if clean_text(item)]
    if parsed is not None and not isinstance(parsed, (dict, list)):
        return [clean_text(parsed)] if clean_text(parsed) else []
    parts = re.split(r"[,;|]", value)
    return [part.strip() for part in parts if part.strip()]


def parse_int_list(value: str) -> list[int]:
    return [
        parsed for item in parse_list(value) if (parsed := parse_int(item)) is not None
    ]


def parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def parse_confidence(value: str) -> float:
    if not value:
        return DEFAULT_CONFIDENCE
    try:
        parsed = float(value.strip().rstrip("%"))
    except ValueError:
        return DEFAULT_CONFIDENCE
    if parsed > 1:
        parsed = parsed / 100
    return max(0.0, min(parsed, 1.0))


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    for candidate in (text, text.replace("/", "-")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m-%d-%Y",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    raise ValueError(f"invalid datetime {value!r}")


def parse_json(value: str) -> Any | None:
    text = value.strip()
    if not text or text[0] not in '[{"':
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def infer_keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.casefold())
    return dedupe(word for word in words if word not in STOPWORDS)[:12]


def first_sentence(value: str) -> str:
    clean = clean_text(value)
    if not clean:
        return ""
    match = re.search(r"(?<=[.!?])\s+", clean)
    if not match:
        return clean
    return clean[: match.start()].strip()


def dedupe(values: Sequence[str] | Any) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        text = clean_text(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            deduped.append(text)
    return deduped


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def truncate(value: str, max_chars: int) -> str:
    clean = clean_text(value)
    if len(clean) <= max_chars:
        return clean
    return f"{clean[: max_chars - 3].rstrip()}..."


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", clean_text(value).casefold()).strip("-")


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().casefold())


if __name__ == "__main__":
    main()
