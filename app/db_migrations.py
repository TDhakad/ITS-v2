from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql import select

from app.schemas import ChatMessage

logger = logging.getLogger(__name__)

Migration = tuple[str, Callable[[Connection], None]]


def run_migrations(engine: Engine) -> None:
    """Apply explicit, idempotent database migrations in order."""
    with engine.begin() as connection:
        _ensure_migration_table(connection)
        applied = set(
            connection.execute(text("SELECT version FROM schema_migrations")).scalars()
        )
        for version, migration in MIGRATIONS:
            if version in applied:
                continue
            logger.info("Applying database migration %s", version)
            migration(connection)
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": version},
            )


def _ensure_migration_table(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(64) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _initial_schema(connection: Connection) -> None:
    from app.db import Base

    Base.metadata.create_all(bind=connection)


def _backfill_ticket_messages(connection: Connection) -> None:
    from app.db import TicketMessageRecord, TicketRecord

    with Session(bind=connection, autoflush=False, expire_on_commit=False) as db:
        records = db.scalars(
            select(TicketRecord).options(selectinload(TicketRecord.messages))
        ).all()
        for record in records:
            if record.messages or not record.conversation:
                continue
            for raw_message in record.conversation:
                message = ChatMessage.model_validate(raw_message)
                db.add(
                    TicketMessageRecord(
                        ticket_id=record.id,
                        role=message.role.value,
                        content=message.content,
                        created_at=message.created_at,
                    )
                )
        db.flush()


def _create_chat_messages(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id VARCHAR(120) NOT NULL,
                thread_id VARCHAR(120) NOT NULL,
                role VARCHAR(40) NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_chat_messages_id ON chat_messages (id)")
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_chat_messages_user_id ON chat_messages (user_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_chat_messages_thread_id ON chat_messages (thread_id)"
        )
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_chat_messages_role ON chat_messages (role)")
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_chat_messages_created_at ON chat_messages (created_at)"
        )
    )


def _drop_deprecated_ticket_link_tables(connection: Connection) -> None:
    # Dynamic insights replaced persisted ticket_kb_links / duplicate_ticket_links.
    # Drop legacy tables if they still exist.
    connection.execute(text("DROP TABLE IF EXISTS ticket_kb_links"))
    connection.execute(text("DROP TABLE IF EXISTS duplicate_ticket_links"))


def _add_agent_response_to_chat_messages(connection: Connection) -> None:
    connection.execute(
        text(
            "ALTER TABLE chat_messages ADD COLUMN agent_response TEXT"
        )
    )


MIGRATIONS: tuple[Migration, ...] = (
    ("0001_initial_schema", _initial_schema),
    ("0002_backfill_ticket_messages", _backfill_ticket_messages),
    ("0003_create_chat_messages", _create_chat_messages),
    ("0004_drop_deprecated_ticket_link_tables", _drop_deprecated_ticket_link_tables),
    ("0005_add_agent_response_to_chat_messages", _add_agent_response_to_chat_messages),
)
