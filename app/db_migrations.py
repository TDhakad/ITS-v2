from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import Engine, inspect, text
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
    if _column_exists(connection, "chat_messages", "agent_response"):
        return
    connection.execute(text("ALTER TABLE chat_messages ADD COLUMN agent_response TEXT"))


def _create_ticket_comments(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ticket_comments (
                id INTEGER NOT NULL PRIMARY KEY,
                ticket_id INTEGER NOT NULL,
                author_user_id INTEGER NOT NULL,
                parent_comment_id INTEGER,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets (id) ON DELETE CASCADE,
                FOREIGN KEY(author_user_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY(parent_comment_id) REFERENCES ticket_comments (id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_ticket_comments_ticket_id ON ticket_comments (ticket_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_ticket_comments_author_user_id ON ticket_comments (author_user_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_ticket_comments_parent_comment_id ON ticket_comments (parent_comment_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_ticket_comments_created_at ON ticket_comments (created_at)"
        )
    )


def _create_background_jobs_and_insights(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS background_jobs (
                id INTEGER NOT NULL PRIMARY KEY,
                kind VARCHAR(80) NOT NULL,
                payload JSON NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                last_error TEXT,
                run_after DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_background_jobs_status_run_after "
            "ON background_jobs (status, run_after)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_background_jobs_kind ON background_jobs (kind)"
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ticket_insights (
                ticket_id INTEGER NOT NULL PRIMARY KEY,
                cache_key VARCHAR(200) NOT NULL,
                payload JSON NOT NULL,
                generated_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets (id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_ticket_insights_cache_key "
            "ON ticket_insights (cache_key)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_ticket_insights_expires_at "
            "ON ticket_insights (expires_at)"
        )
    )


def _create_hot_path_indexes(connection: Connection) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_user_thread_created "
        "ON chat_messages (user_id, thread_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_user_thread_role_created "
        "ON chat_messages (user_id, thread_id, role, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_project_members_user_project "
        "ON project_members (user_id, project_id)",
        "CREATE INDEX IF NOT EXISTS ix_tickets_project_status_created "
        "ON tickets (project_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_tickets_project_priority_created "
        "ON tickets (project_id, suggested_priority, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_tickets_project_category_created "
        "ON tickets (project_id, category, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_ticket_comments_ticket_created "
        "ON ticket_comments (ticket_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_ticket_tags_ticket_tag "
        "ON ticket_tags (ticket_id, tag_id)",
    ]
    for statement in statements:
        connection.execute(text(statement))


def _column_exists(connection: Connection, table_name: str, column_name: str) -> bool:
    inspector = inspect(connection)
    try:
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        return column_name in columns
    except Exception:
        return False


MIGRATIONS: tuple[Migration, ...] = (
    ("0001_initial_schema", _initial_schema),
    ("0002_backfill_ticket_messages", _backfill_ticket_messages),
    ("0003_create_chat_messages", _create_chat_messages),
    ("0004_drop_deprecated_ticket_link_tables", _drop_deprecated_ticket_link_tables),
    ("0005_add_agent_response_to_chat_messages", _add_agent_response_to_chat_messages),
    ("0006_create_ticket_comments", _create_ticket_comments),
    ("0007_create_background_jobs_and_insights", _create_background_jobs_and_insights),
    ("0008_create_hot_path_indexes", _create_hot_path_indexes),
)
