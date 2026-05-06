from __future__ import annotations

import logging
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    cast,
    create_engine,
    event,
    or_,
    select,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)
from sqlalchemy.types import JSON

from app.schemas import (
    DEFAULT_TAG_SLUGS,
    TAG_COLORS,
    ChatMessage,
    Environment,
    GuardrailDecision,
    Priority,
    ProjectAccessLevel,
    ProjectCreate,
    ResolutionData,
    TicketCategory,
    TicketCreate,
    TicketIntelligence,
    TicketRead,
    TicketStatus,
    UserClearance,
    UserRole,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)


class TicketVectorUnavailableError(RuntimeError):
    """Raised when a configured ticket vector search cannot complete."""


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


settings = get_settings()


def _engine_kwargs(database_url: str) -> dict[str, Any]:
    if database_url.startswith("sqlite"):
        return {
            "connect_args": {
                "check_same_thread": False,
                "timeout": settings.sqlite_connect_timeout,
            }
        }
    return {}


engine = create_engine(
    settings.database_url,
    echo=settings.sql_echo,
    future=True,
    **_engine_kwargs(settings.database_url),
)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)


@event.listens_for(engine, "connect")
def set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    del connection_record
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            logger.debug("SQLite WAL mode unavailable for this connection")
        cursor.close()


# ── Auth models ───────────────────────────────────────────────────────────────


class UserRecord(Base):
    """Stores application user accounts used for authentication and authorization.

    This table represents people or service users who can sign in and access the
    platform. It tracks account lifecycle state (active/inactive), role-based
    permissions (for example user/agent/admin), and clearance level for data
    visibility boundaries.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        comment="Primary key for the user record.",
    )
    email: Mapped[str] = mapped_column(
        String(254),
        unique=True,
        index=True,
        nullable=False,
        comment="Unique login email stored in lowercase.",
    )
    display_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="Display name shown in the application UI.",
    )
    hashed_password: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Password hash; never stores plaintext passwords.",
    )
    role: Mapped[str] = mapped_column(
        String(40),
        default=UserRole.USER.value,
        index=True,
        comment="Authorization role. Available options: user, agent, admin.",
    )
    clearance: Mapped[str] = mapped_column(
        String(40),
        default=UserClearance.PUBLIC.value,
        comment="Data visibility clearance. Available options: public, internal, restricted.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        index=True,
        comment="Soft-delete/enable flag for account access.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        comment="UTC timestamp when the user account was created.",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp of the most recent successful login.",
    )

    project_memberships: Mapped[list[ProjectMemberRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list[AuthSessionRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSessionRecord(Base):
    """Stores active and historical login sessions for signed-in users.

    Each row corresponds to one issued authentication session token with an
    expiry timestamp. This model supports session validation, logout, and
    automatic invalidation of sessions that are expired or removed.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="Opaque session token used as the session primary key.",
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to users.id for session ownership.",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        comment="UTC timestamp when the session expires.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        comment="UTC timestamp when the session was created.",
    )
    user_agent: Mapped[str] = mapped_column(
        String(512),
        default="",
        comment="User-Agent captured at login time.",
    )
    ip_address: Mapped[str] = mapped_column(
        String(64),
        default="",
        comment="Client IP address captured at login time.",
    )

    user: Mapped[UserRecord] = relationship(back_populates="sessions")


class ChatMessageRecord(Base):
    """Persists general chat assistant conversations outside ticket workflows.

    This table stores the message history for chat threads used in the assistant
    experience. It is primarily used to reconstruct prior context, display chat
    history, and provide previews of recent conversations.
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="Primary key for the chat message row.",
    )
    user_id: Mapped[str] = mapped_column(
        String(120),
        index=True,
        comment="Application user identifier for the chat thread.",
    )
    thread_id: Mapped[str] = mapped_column(
        String(120),
        index=True,
        comment="Conversation thread identifier.",
    )
    role: Mapped[str] = mapped_column(
        String(40),
        index=True,
        comment="Message author role. Available options: user, assistant, system.",
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="Raw chat message content.",
    )
    agent_response: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="JSON-serialised AgentResponse (chart config etc.) for assistant messages.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        comment="UTC timestamp when the chat message was stored.",
    )


# ── Project models ─────────────────────────────────────────────────────────────


class ProjectRecord(Base):
    """Represents tenant-like project workspaces in the ITS platform.

    Projects are used to group operational data and access scope for teams.
    Records here define the project identity and lifecycle state, and serve as
    the anchor for memberships and KB visibility links.
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        comment="Primary key for the project record.",
    )
    name: Mapped[str] = mapped_column(
        String(120),
        unique=True,
        nullable=False,
        comment="Unique human-friendly project name.",
    )
    slug: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
        nullable=False,
        comment="Unique URL-safe project identifier.",
    )
    description: Mapped[str] = mapped_column(
        Text,
        default="",
        comment="Project description shown in management views.",
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Owning user id; nullable when owner is removed.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        index=True,
        comment="Soft-enable flag for project availability.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        comment="UTC timestamp when the project was created.",
    )

    members: Mapped[list[ProjectMemberRecord]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    kb_links: Mapped[list[KBProjectLinkRecord]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMemberRecord(Base):
    """Maps users to projects with a project-specific access level.

    This junction table controls who can access a given project and at what
    capability level (such as viewer/member/owner). The unique project-user
    constraint ensures each user has a single effective membership per project.
    """

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_user"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="Primary key for project membership row.",
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to projects.id.",
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to users.id.",
    )
    access_level: Mapped[str] = mapped_column(
        String(40),
        default=ProjectAccessLevel.MEMBER.value,
        comment="Project access level. Available options: viewer, member, owner.",
    )

    project: Mapped[ProjectRecord] = relationship(back_populates="members")
    user: Mapped[UserRecord] = relationship(back_populates="project_memberships")


# ── Tag models ─────────────────────────────────────────────────────────────────


class TagRecord(Base):
    """Defines reusable classification tags shared across tickets and KB links.

    Tags provide a normalized vocabulary for categorization, filtering, and
    analytics. They are intended to be stable labels (for example impact areas)
    that can be attached to tickets and knowledge-base documents.
    """

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        comment="Primary key for tag record.",
    )
    name: Mapped[str] = mapped_column(
        String(60),
        unique=True,
        nullable=False,
        comment="Unique display name for the tag.",
    )
    slug: Mapped[str] = mapped_column(
        String(60),
        unique=True,
        index=True,
        nullable=False,
        comment="Unique URL-safe tag identifier.",
    )
    color: Mapped[str] = mapped_column(
        String(20),
        default="#64748b",
        comment="Hex color code used for tag badges.",
    )
    description: Mapped[str] = mapped_column(
        String(500),
        default="",
        comment="Optional tag description.",
    )

    ticket_links: Mapped[list[TicketTagRecord]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )
    kb_links: Mapped[list[KBTagRecord]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )


class TicketTagRecord(Base):
    """Junction table linking support tickets to classification tags.

    This model enables many-to-many tag assignment on tickets so tickets can be
    filtered, searched, and summarized by multiple thematic labels.
    """

    __tablename__ = "ticket_tags"
    __table_args__ = (UniqueConstraint("ticket_id", "tag_id", name="uq_ticket_tag"),)

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="Primary key for ticket-tag link row.",
    )
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to tickets.id.",
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to tags.id.",
    )

    ticket: Mapped[TicketRecord] = relationship(back_populates="tag_links")
    tag: Mapped[TagRecord] = relationship(back_populates="ticket_links")


class KBTagRecord(Base):
    """Junction table linking knowledge-base documents to tags.

    It supports tag-driven retrieval and categorization of KB content so the
    assistant can discover relevant documentation by topic.
    """

    __tablename__ = "kb_tags"
    __table_args__ = (UniqueConstraint("kb_id", "tag_id", name="uq_kb_tag"),)

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="Primary key for KB-tag link row.",
    )
    kb_id: Mapped[str] = mapped_column(
        String(255),
        index=True,
        comment="Knowledge-base document identifier.",
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to tags.id.",
    )

    tag: Mapped[TagRecord] = relationship(back_populates="kb_links")


class KBProjectLinkRecord(Base):
    """Junction table that scopes KB documents to one or more projects.

    This association is used for project-level data isolation so users only see
    knowledge-base documents that are linked to projects they can access.
    """

    __tablename__ = "kb_project_links"
    __table_args__ = (UniqueConstraint("kb_id", "project_id", name="uq_kb_project"),)

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="Primary key for KB-project link row.",
    )
    kb_id: Mapped[str] = mapped_column(
        String(255),
        index=True,
        comment="Knowledge-base document identifier.",
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to projects.id.",
    )

    project: Mapped[ProjectRecord] = relationship(back_populates="kb_links")


# ── Ticket model ───────────────────────────────────────────────────────────────


class TicketRecord(Base):
    """Primary incident/support ticket entity for operational issue tracking.

    Each row is a ticket raised in the ITS workflow and stores its current
    lifecycle state, AI-derived triage metadata, and resolution context.
    Typical status values in this table include Open, Triaged, In Progress,
    Resolved, and Closed, which represent the ticket's movement from intake to
    completion.
    """

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        comment="Primary key for ticket record.",
    )
    status: Mapped[str] = mapped_column(
        String(40),
        default=TicketStatus.OPEN.value,
        index=True,
        comment=(
            "Lifecycle status of the ticket. Available options: Open, Triaged, "
            "In Progress, Resolved, Closed."
        ),
    )
    user_id: Mapped[str] = mapped_column(
        String(120),
        default="anonymous",
        index=True,
        comment="Creator identifier for the ticket.",
    )
    thread_id: Mapped[str] = mapped_column(
        String(120),
        index=True,
        comment="Conversation thread id linked to ticket creation.",
    )
    app_name: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
        comment="Application/service name related to the issue.",
    )
    environment: Mapped[str] = mapped_column(
        String(40),
        default=Environment.UNKNOWN.value,
        index=True,
        comment="Target runtime environment. Available options: production, staging, development, unknown.",
    )
    user_clearance: Mapped[str] = mapped_column(
        String(40),
        default=UserClearance.PUBLIC.value,
        comment="Clearance level attached to the ticket. Available options: public, internal, restricted.",
    )

    category: Mapped[str] = mapped_column(
        String(128),
        default=TicketCategory.INFRA.value,
        index=True,
        comment="Classified ticket category. Available options: Bug, Feature, UI, Infra, Hardware.",
    )
    suggested_priority: Mapped[str] = mapped_column(
        String(40),
        default=Priority.MEDIUM.value,
        index=True,
        comment="Suggested impact priority. Available options: Low, Medium, High, Critical.",
    )
    summary: Mapped[str] = mapped_column(
        Text,
        default="",
        comment="Normalized short summary of the ticket issue.",
    )
    keywords: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        comment="Extracted keyword list used for search and analytics.",
    )

    intelligence: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        comment="Structured classification and triage metadata.",
    )
    resolution: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        comment="Structured resolution suggestions and linked references.",
    )
    guardrail: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Optional safety/guardrail decision payload.",
    )
    conversation: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        comment="Legacy JSON conversation cache retained for backward compatibility.",
    )
    raw_context: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        comment="Original request context captured at ticket creation.",
    )

    # Project association
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Owning project id for tenant scoping; nullable for legacy tickets.",
    )

    # Legacy column retained so the generated SQLite DB from earlier scaffolding remains usable.
    sentiment: Mapped[str] = mapped_column(
        String(40),
        default="Calm",
        comment="Legacy sentiment label retained for older database compatibility.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        comment="UTC timestamp when the ticket was created.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        comment="UTC timestamp of the latest ticket update.",
    )

    messages: Mapped[list[TicketMessageRecord]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketMessageRecord.created_at",
    )
    comments: Mapped[list[TicketCommentRecord]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketCommentRecord.created_at",
    )
    tag_links: Mapped[list[TicketTagRecord]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
    )


class TicketMessageRecord(Base):
    """Stores ordered conversation messages that belong to a specific ticket.

    This table is the authoritative message timeline for ticket discussions and
    resolution collaboration. It preserves role-based turns (user/assistant/
    system) for auditability and context reconstruction.
    """

    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="Primary key for the ticket message row.",
    )
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to tickets.id.",
    )
    role: Mapped[str] = mapped_column(
        String(40),
        index=True,
        comment="Message author role. Available options: user, assistant, system.",
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="Message text for the ticket conversation.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        comment="UTC timestamp when the ticket message was created.",
    )

    ticket: Mapped[TicketRecord] = relationship(back_populates="messages")


class TicketCommentRecord(Base):
    """Stores user and admin comments for a ticket, including threaded replies."""

    __tablename__ = "ticket_comments"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="Primary key for the ticket comment row.",
    )
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"),
        index=True,
        comment="Foreign key to tickets.id.",
    )
    author_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        comment="Author user id for this comment.",
    )
    parent_comment_id: Mapped[int | None] = mapped_column(
        ForeignKey("ticket_comments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Optional parent comment id for threaded replies.",
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="Comment text.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        comment="UTC timestamp when the comment was created.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        comment="UTC timestamp when the comment was last updated.",
    )

    ticket: Mapped[TicketRecord] = relationship(back_populates="comments")
    author: Mapped[UserRecord] = relationship()
    parent: Mapped[TicketCommentRecord | None] = relationship(
        "TicketCommentRecord",
        remote_side="TicketCommentRecord.id",
        back_populates="children",
    )
    children: Mapped[list[TicketCommentRecord]] = relationship(
        "TicketCommentRecord",
        back_populates="parent",
        cascade="all, delete-orphan",
        single_parent=True,
        order_by="TicketCommentRecord.created_at",
    )


class BackgroundJobRecord(Base):
    """Durable queue for slow external work owned by the web application."""

    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict
    )
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TicketInsightRecord(Base):
    """Caches expensive ticket insight payloads so read endpoints stay predictable."""

    __tablename__ = "ticket_insights"

    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True
    )
    cache_key: Mapped[str] = mapped_column(String(200), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


def ensure_database_directory() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    url = make_url(settings.database_url)
    if not url.database or url.database == ":memory:":
        return
    path = Path(url.database)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    ensure_database_directory()
    from app.db_migrations import run_migrations

    run_migrations(engine)
    with SessionLocal() as db:
        seed_default_tags(db)


def get_session() -> Generator[Session, None, None]:
    ensure_database_directory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def add_chat_turn_messages(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    user_message: str,
    assistant_message: str,
    agent_response_json: str | None = None,
) -> None:
    now = utcnow()
    db.add_all(
        [
            ChatMessageRecord(
                user_id=user_id,
                thread_id=thread_id,
                role="user",
                content=user_message,
                created_at=now,
            ),
            ChatMessageRecord(
                user_id=user_id,
                thread_id=thread_id,
                role="assistant",
                content=assistant_message,
                agent_response=agent_response_json,
                created_at=utcnow(),
            ),
        ]
    )
    db.commit()


def list_chat_threads(
    db: Session,
    *,
    user_id: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return the most-recently-active chat threads for a user with a preview."""
    from sqlalchemy import func

    inner = aliased(ChatMessageRecord)
    # Correlated subquery: content of the MOST RECENT user message per thread.
    preview_subq = (
        select(inner.content)
        .where(
            inner.user_id == user_id,
            inner.thread_id == ChatMessageRecord.thread_id,
            inner.role == "user",
        )
        .order_by(inner.created_at.desc())
        .limit(1)
        .correlate(ChatMessageRecord)
        .scalar_subquery()
    )
    stmt = (
        select(
            ChatMessageRecord.thread_id,
            func.max(ChatMessageRecord.created_at).label("last_at"),
            func.count(ChatMessageRecord.id).label("message_count"),
            preview_subq.label("preview"),
        )
        .where(ChatMessageRecord.user_id == user_id)
        .group_by(ChatMessageRecord.thread_id)
        .order_by(func.max(ChatMessageRecord.created_at).desc())
        .limit(limit)
    )
    rows = db.execute(stmt).mappings().all()
    return [
        {
            "thread_id": row["thread_id"],
            "last_at": row["last_at"].isoformat() if row["last_at"] else None,
            "message_count": row["message_count"],
            "preview": (row["preview"] or "")[:80],
        }
        for row in rows
    ]


def get_user_project_ids(db: Session, user_id: int) -> list[int]:
    """Return IDs of all active projects the user is a member of."""
    return list(
        db.scalars(
            select(ProjectMemberRecord.project_id).where(
                ProjectMemberRecord.user_id == user_id
            )
        ).all()
    )


def list_chat_messages(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    limit: int = 200,
) -> list[ChatMessageRecord]:
    stmt = (
        select(ChatMessageRecord)
        .where(
            ChatMessageRecord.user_id == user_id,
            ChatMessageRecord.thread_id == thread_id,
        )
        .order_by(ChatMessageRecord.created_at.asc(), ChatMessageRecord.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def delete_chat_thread_messages(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
) -> int:
    """Delete all messages for one user thread. Returns deleted row count."""
    records = list(
        db.scalars(
            select(ChatMessageRecord).where(
                ChatMessageRecord.user_id == user_id,
                ChatMessageRecord.thread_id == thread_id,
            )
        ).all()
    )
    if not records:
        return 0
    for record in records:
        db.delete(record)
    db.commit()
    return len(records)


def _ticket_read_options() -> tuple[Any, ...]:
    return (
        selectinload(TicketRecord.messages),
        selectinload(TicketRecord.tag_links).selectinload(TicketTagRecord.tag),
    )


def _tag_slugs_for_record(record: TicketRecord) -> list[str]:
    return [link.tag.slug for link in (record.tag_links or []) if link.tag]


def _conversation_for_record(record: TicketRecord) -> list[ChatMessage]:
    if record.messages:
        return [
            ChatMessage.model_validate(
                {
                    "role": message.role,
                    "content": message.content,
                    "created_at": message.created_at,
                }
            )
            for message in record.messages
        ]
    return [
        ChatMessage.model_validate(message) for message in record.conversation or []
    ]


def _ticket_to_read(record: TicketRecord) -> TicketRead:
    # Resolution insights are derived at read-time via the insights pipeline.
    # Ignore stored dynamic links to avoid serving stale duplicate/KB data.
    return TicketRead(
        id=record.id,
        user_id=record.user_id,
        thread_id=record.thread_id,
        status=TicketStatus(record.status),
        app_name=record.app_name,
        environment=Environment(record.environment),
        user_clearance=UserClearance(record.user_clearance),
        project_id=record.project_id,
        tag_slugs=_tag_slugs_for_record(record),
        intelligence=TicketIntelligence.model_validate(record.intelligence),
        resolution=ResolutionData(),
        conversation=_conversation_for_record(record),
        guardrail=(
            GuardrailDecision.model_validate(record.guardrail)
            if record.guardrail
            else None
        ),
        raw_context=record.raw_context or {},
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def create_ticket(
    db: Session, ticket: TicketCreate, *, index_vector: bool = True
) -> TicketRead:
    record = _add_ticket_record(db, ticket)
    db.commit()
    ticket_read = get_ticket(db, record.id)
    if ticket_read is None:
        raise RuntimeError(f"Created ticket {record.id} could not be reloaded")
    if index_vector:
        _index_ticket_vector(ticket_read)
    return ticket_read


def create_tickets(
    db: Session,
    tickets: Sequence[TicketCreate],
    *,
    index_vectors: bool = True,
    vector_batch_size: int = 100,
) -> list[TicketRead]:
    records = [_add_ticket_record(db, ticket) for ticket in tickets]
    db.commit()
    ticket_reads = get_tickets_by_ids(db, [record.id for record in records])
    if index_vectors:
        _index_ticket_vectors(ticket_reads, batch_size=vector_batch_size)
    return ticket_reads


def _add_ticket_record(db: Session, ticket: TicketCreate) -> TicketRecord:
    intelligence = ticket.intelligence.model_dump(mode="json")
    resolution = ResolutionData().model_dump(mode="json")
    timestamp_fields: dict[str, datetime] = {}
    if ticket.created_at:
        timestamp_fields["created_at"] = ticket.created_at
    if ticket.updated_at:
        timestamp_fields["updated_at"] = ticket.updated_at
    elif ticket.created_at:
        timestamp_fields["updated_at"] = ticket.created_at

    record = TicketRecord(
        status=ticket.status.value,
        user_id=ticket.user_id,
        thread_id=ticket.thread_id,
        app_name=ticket.app_name,
        environment=ticket.environment.value,
        user_clearance=ticket.user_clearance.value,
        project_id=ticket.project_id,
        category=ticket.intelligence.category.value,
        suggested_priority=ticket.intelligence.suggested_priority.value,
        summary=ticket.intelligence.summary,
        keywords=ticket.intelligence.keywords,
        intelligence=intelligence,
        resolution=resolution,
        guardrail=(
            ticket.guardrail.model_dump(mode="json") if ticket.guardrail else None
        ),
        # ticket_messages is authoritative; the JSON column remains only for legacy DBs.
        conversation=[],
        raw_context=ticket.raw_context,
        **timestamp_fields,
    )
    db.add(record)
    db.flush()

    for message in ticket.conversation:
        db.add(
            TicketMessageRecord(
                ticket_id=record.id,
                role=message.role.value,
                content=message.content,
                created_at=message.created_at,
            )
        )

    # Attach tags by slug — skip unknown slugs silently.
    if ticket.tag_slugs:
        tags = db.scalars(
            select(TagRecord).where(TagRecord.slug.in_(ticket.tag_slugs))
        ).all()
        for tag in tags:
            db.add(TicketTagRecord(ticket_id=record.id, tag_id=tag.id))

    return record


def get_ticket(db: Session, ticket_id: int) -> TicketRead | None:
    record = db.scalars(
        select(TicketRecord)
        .options(*_ticket_read_options())
        .where(TicketRecord.id == ticket_id)
    ).first()
    return _ticket_to_read(record) if record else None


def get_tickets_by_ids(db: Session, ticket_ids: Sequence[int]) -> list[TicketRead]:
    if not ticket_ids:
        return []
    records = (
        db.scalars(
            select(TicketRecord)
            .options(*_ticket_read_options())
            .where(TicketRecord.id.in_(ticket_ids))
        )
        .unique()
        .all()
    )
    by_id = {record.id: _ticket_to_read(record) for record in records}
    return [by_id[ticket_id] for ticket_id in ticket_ids if ticket_id in by_id]


def list_tickets(
    db: Session,
    status: TicketStatus | None = None,
    *,
    project_id: int | None = None,
    project_ids: list[int] | None = None,
    tag_slug: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    user_id: str | None = None,
    limit: int = 100,
) -> list[TicketRead]:
    stmt = select(TicketRecord).order_by(TicketRecord.created_at.desc())
    if status:
        stmt = stmt.where(TicketRecord.status == status.value)
    if project_id is not None:
        stmt = stmt.where(TicketRecord.project_id == project_id)
    elif project_ids is not None:
        stmt = stmt.where(TicketRecord.project_id.in_(project_ids))
    if priority:
        stmt = stmt.where(TicketRecord.suggested_priority == priority)
    if category:
        stmt = stmt.where(TicketRecord.category == category)
    if user_id:
        stmt = stmt.where(TicketRecord.user_id == user_id)
    if tag_slug:
        stmt = (
            stmt.join(TicketTagRecord, TicketTagRecord.ticket_id == TicketRecord.id)
            .join(TagRecord, TagRecord.id == TicketTagRecord.tag_id)
            .where(TagRecord.slug == tag_slug)
        )
    stmt = stmt.limit(limit)
    stmt = stmt.options(*_ticket_read_options())
    return [_ticket_to_read(record) for record in db.scalars(stmt).unique().all()]


def list_ticket_comments(db: Session, ticket_id: int) -> list[TicketCommentRecord]:
    stmt = (
        select(TicketCommentRecord)
        .options(selectinload(TicketCommentRecord.author))
        .where(TicketCommentRecord.ticket_id == ticket_id)
        .order_by(TicketCommentRecord.created_at.asc(), TicketCommentRecord.id.asc())
    )
    return list(db.scalars(stmt).all())


def get_ticket_comment(
    db: Session,
    *,
    ticket_id: int,
    comment_id: int,
) -> TicketCommentRecord | None:
    return db.scalars(
        select(TicketCommentRecord)
        .options(selectinload(TicketCommentRecord.author))
        .where(
            TicketCommentRecord.ticket_id == ticket_id,
            TicketCommentRecord.id == comment_id,
        )
    ).first()


def create_ticket_comment(
    db: Session,
    *,
    ticket_id: int,
    author_user_id: int,
    content: str,
    parent_comment_id: int | None = None,
) -> TicketCommentRecord:
    if parent_comment_id is not None:
        parent = db.scalars(
            select(TicketCommentRecord).where(
                TicketCommentRecord.id == parent_comment_id,
                TicketCommentRecord.ticket_id == ticket_id,
            )
        ).first()
        if parent is None:
            raise ValueError("Parent comment was not found for this ticket.")

    record = TicketCommentRecord(
        ticket_id=ticket_id,
        author_user_id=author_user_id,
        parent_comment_id=parent_comment_id,
        content=content,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return get_ticket_comment(db, ticket_id=ticket_id, comment_id=record.id) or record


def update_ticket_comment(
    db: Session,
    *,
    ticket_id: int,
    comment_id: int,
    content: str,
) -> TicketCommentRecord | None:
    record = get_ticket_comment(db, ticket_id=ticket_id, comment_id=comment_id)
    if record is None:
        return None
    record.content = content
    record.updated_at = utcnow()
    db.commit()
    db.refresh(record)
    return get_ticket_comment(db, ticket_id=ticket_id, comment_id=record.id) or record


def delete_ticket_comment(
    db: Session,
    *,
    ticket_id: int,
    comment_id: int,
) -> bool:
    record = get_ticket_comment(db, ticket_id=ticket_id, comment_id=comment_id)
    if record is None:
        return False
    db.delete(record)
    db.commit()
    return True


def enqueue_background_job(
    db: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    run_after: datetime | None = None,
    max_attempts: int = 5,
) -> BackgroundJobRecord:
    record = BackgroundJobRecord(
        kind=kind,
        payload=payload,
        run_after=run_after or utcnow(),
        max_attempts=max(1, max_attempts),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def claim_next_background_job(db: Session) -> BackgroundJobRecord | None:
    now = utcnow()
    stale_running_before = now - timedelta(minutes=15)
    record = db.scalars(
        select(BackgroundJobRecord)
        .where(
            or_(
                BackgroundJobRecord.status == "pending",
                and_(
                    BackgroundJobRecord.status == "running",
                    BackgroundJobRecord.updated_at <= stale_running_before,
                ),
            ),
            BackgroundJobRecord.run_after <= now,
            BackgroundJobRecord.attempts < BackgroundJobRecord.max_attempts,
        )
        .order_by(BackgroundJobRecord.run_after.asc(), BackgroundJobRecord.id.asc())
        .limit(1)
    ).first()
    if record is None:
        return None
    record.status = "running"
    record.attempts += 1
    record.updated_at = now
    db.commit()
    db.refresh(record)
    return record


def complete_background_job(db: Session, job: BackgroundJobRecord) -> None:
    job.status = "succeeded"
    job.last_error = None
    job.updated_at = utcnow()
    db.commit()


def fail_background_job(
    db: Session,
    job: BackgroundJobRecord,
    error: str,
    *,
    retry_delay_seconds: int = 60,
) -> None:
    now = utcnow()
    job.last_error = error[:2_000]
    job.updated_at = now
    if job.attempts >= job.max_attempts:
        job.status = "failed"
    else:
        delay = min(max(retry_delay_seconds, 5) * max(job.attempts, 1), 900)
        job.status = "pending"
        job.run_after = now + timedelta(seconds=delay)
    db.commit()


def get_cached_ticket_insight(
    db: Session,
    *,
    ticket_id: int,
    cache_key: str,
) -> dict[str, Any] | None:
    record = db.get(TicketInsightRecord, ticket_id)
    if record is None or record.cache_key != cache_key:
        return None
    now = utcnow()
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    if expires_at < now:
        return None
    return dict(record.payload or {})


def save_ticket_insight(
    db: Session,
    *,
    ticket_id: int,
    cache_key: str,
    payload: dict[str, Any],
    ttl_seconds: int = 900,
) -> TicketInsightRecord:
    now = utcnow()
    record = db.get(TicketInsightRecord, ticket_id)
    if record is None:
        record = TicketInsightRecord(ticket_id=ticket_id, cache_key=cache_key)
        db.add(record)
    record.cache_key = cache_key
    record.payload = payload
    record.generated_at = now
    record.expires_at = now + timedelta(seconds=max(ttl_seconds, 30))
    db.commit()
    db.refresh(record)
    return record


def search_tickets(
    db: Session,
    query: str,
    *,
    user_id: str | None = None,
    project_id: int | None = None,
    tag_slugs: list[str] | None = None,
    status: str | None = None,
    priority: str | None = None,
    limit: int = 10,
    use_vector: bool = True,
) -> list[dict[str, Any]]:
    """Semantic ticket search with keyword fallback.

    Pinecone handles content-level recall when the ticket vector index is
    configured. SQL keyword search remains the zero-dependency local fallback.
    """
    if not query or not query.strip():
        return []

    vector_results = (
        _search_ticket_vectors(
            query,
            user_id=user_id,
            project_id=project_id,
            tag_slugs=tag_slugs,
            status=status,
            priority=priority,
            limit=limit,
        )
        if use_vector
        else []
    )

    tokens = [token.casefold() for token in query.split() if len(token) > 2]
    if not tokens:
        return vector_results[:limit]

    conditions = _ticket_match_conditions(tokens)
    stmt = (
        select(TicketRecord)
        .options(*_ticket_read_options())
        .where(or_(*conditions))
        .order_by(TicketRecord.created_at.desc())
        .limit(max(limit * 8, 50))
    )
    if user_id:
        stmt = stmt.where(TicketRecord.user_id == user_id)
    if project_id is not None:
        stmt = stmt.where(TicketRecord.project_id == project_id)
    if status:
        stmt = stmt.where(TicketRecord.status == status)
    if priority:
        stmt = stmt.where(TicketRecord.suggested_priority == priority)
    if tag_slugs:
        stmt = (
            stmt.join(TicketTagRecord, TicketTagRecord.ticket_id == TicketRecord.id)
            .join(TagRecord, TagRecord.id == TicketTagRecord.tag_id)
            .where(TagRecord.slug.in_(tag_slugs))
        )

    results: list[dict[str, Any]] = []
    for record in db.scalars(stmt).unique().all():
        hits = _ticket_search_score(record, tokens)
        if hits > 0:
            results.append(
                {
                    "ticket_id": record.id,
                    "summary": record.summary,
                    "status": record.status,
                    "priority": record.suggested_priority,
                    "category": record.category,
                    "app_name": record.app_name,
                    "user_id": record.user_id,
                    "project_id": record.project_id,
                    "tags": _tag_slugs_for_record(record),
                    "created_at": (
                        record.created_at.isoformat() if record.created_at else None
                    ),
                    "score": hits,
                    "source": "sql",
                }
            )

    results.sort(key=lambda r: r["score"], reverse=True)
    if not vector_results:
        return results[:limit]

    merged_by_id: dict[int, dict[str, Any]] = {}
    for result in vector_results:
        item = dict(result)
        item["vector_score"] = float(item.get("score") or 0.0)
        item["keyword_score"] = 0
        merged_by_id[int(item["ticket_id"])] = item

    for result in results:
        ticket_id = int(result["ticket_id"])
        keyword_score = int(result.get("score") or 0)
        existing = merged_by_id.get(ticket_id)
        if existing is None:
            item = dict(result)
            item["keyword_score"] = keyword_score
            item["vector_score"] = 0.0
            merged_by_id[ticket_id] = item
            continue

        existing.update(result)
        existing["source"] = "hybrid"
        existing["keyword_score"] = keyword_score
        existing["vector_score"] = float(existing.get("vector_score") or 0.0)

    merged = list(merged_by_id.values())
    merged.sort(
        key=lambda item: (
            int(item.get("keyword_score") or 0),
            float(item.get("vector_score") or 0.0),
            str(item.get("created_at") or ""),
        ),
        reverse=True,
    )
    return merged[:limit]


def find_recent_similar_tickets(
    query: str,
    *,
    exclude_ticket_id: int | None = None,
    project_id: int | None = None,
    limit: int = 10,
    candidate_limit: int = 40,
) -> list[dict[str, Any]]:
    """Return vector-similar tickets ordered by most recent created_at."""
    if not query or not query.strip():
        return []

    try:
        candidates = _search_ticket_vectors(
            query,
            project_id=project_id,
            exclude_ticket_id=exclude_ticket_id,
            limit=max(limit, candidate_limit),
        )
    except TicketVectorUnavailableError:
        logger.warning(
            "Skipping recent similar ticket search because vector search failed"
        )
        return []

    results: list[dict[str, Any]] = []
    for candidate in candidates:
        created_at = str(candidate.get("created_at") or "")
        results.append(
            {
                "ticket_id": candidate.get("ticket_id"),
                "summary": candidate.get("summary"),
                "status": candidate.get("status"),
                "priority": candidate.get("priority"),
                "category": candidate.get("category"),
                "score": candidate.get("score", 0.0),
                "created_at": created_at,
            }
        )

    # ISO-8601 timestamps sort lexicographically in chronological order.
    results.sort(
        key=lambda item: (item.get("created_at") or "", item.get("score") or 0.0),
        reverse=True,
    )
    return results[:limit]


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ticket_match_conditions(tokens: list[str]) -> list[Any]:
    keyword_text = cast(TicketRecord.keywords, String)
    conditions: list[Any] = []
    for token in tokens:
        pattern = _like_pattern(token)
        conditions.extend(
            [
                TicketRecord.summary.ilike(pattern, escape="\\"),
                TicketRecord.category.ilike(pattern, escape="\\"),
                TicketRecord.app_name.ilike(pattern, escape="\\"),
                TicketRecord.environment.ilike(pattern, escape="\\"),
                keyword_text.ilike(pattern, escape="\\"),
            ]
        )
    return conditions


def _ticket_search_score(record: TicketRecord, tokens: list[str]) -> int:
    combined = " ".join(
        [
            record.summary or "",
            " ".join(record.keywords or []),
            record.category or "",
            record.app_name or "",
            record.environment or "",
        ]
    ).casefold()
    return sum(1 for token in tokens if token in combined)


def _index_ticket_vector(ticket: TicketRead) -> None:
    try:
        from app.ticket_vector import index_ticket

        index_ticket(ticket)
    except Exception as exc:
        logger.warning(
            "Ticket vector indexing failed for ticket %s: %s", ticket.id, exc
        )


def _index_ticket_vectors(
    tickets: Sequence[TicketRead], *, batch_size: int = 100
) -> None:
    if not tickets:
        return
    try:
        from app.ticket_vector import index_tickets

        index_tickets(tickets, reset=False, batch_size=batch_size)
    except Exception as exc:
        logger.warning(
            "Ticket vector batch indexing failed for %s tickets: %s", len(tickets), exc
        )


def _search_ticket_vectors(
    query: str,
    *,
    user_id: str | None = None,
    project_id: int | None = None,
    tag_slugs: list[str] | None = None,
    status: str | None = None,
    priority: str | None = None,
    exclude_ticket_id: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not settings.pinecone_api_key:
        return []

    try:
        from app.ticket_vector import search_ticket_vectors, ticket_vector_result_to_api
    except Exception as exc:
        raise TicketVectorUnavailableError(
            "Ticket vector search dependency unavailable"
        ) from exc

    try:
        results = search_ticket_vectors(
            query,
            user_id=user_id,
            project_id=project_id,
            tag_slugs=tag_slugs,
            status=status,
            priority=priority,
            exclude_ticket_id=exclude_ticket_id,
            limit=limit,
        )
    except Exception as exc:
        raise TicketVectorUnavailableError("Ticket vector search failed") from exc

    return [ticket_vector_result_to_api(result) for result in results]


# ── User helpers ───────────────────────────────────────────────────────────────


def get_user_by_email(db: Session, email: str) -> UserRecord | None:
    return db.scalars(
        select(UserRecord).where(UserRecord.email == email.casefold())
    ).first()


def get_user_by_id(db: Session, user_id: int) -> UserRecord | None:
    return db.get(UserRecord, user_id)


def create_user(
    db: Session,
    email: str,
    display_name: str,
    hashed_password: str,
    role: str = UserRole.USER.value,
    clearance: str = UserClearance.PUBLIC.value,
) -> UserRecord:
    record = UserRecord(
        email=email.casefold(),
        display_name=display_name,
        hashed_password=hashed_password,
        role=role,
        clearance=clearance,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def record_login(db: Session, user: UserRecord) -> None:
    user.last_login_at = utcnow()
    db.commit()


# ── Session helpers ────────────────────────────────────────────────────────────


def create_auth_session(
    db: Session,
    user_id: int,
    token: str,
    expires_at: datetime,
    user_agent: str = "",
    ip_address: str = "",
) -> AuthSessionRecord:
    # Strip timezone so SQLite round-trips as naive UTC consistently.
    naive_expires = expires_at.replace(tzinfo=None) if expires_at.tzinfo else expires_at
    record = AuthSessionRecord(
        id=token,
        user_id=user_id,
        expires_at=naive_expires,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(record)
    db.commit()
    return record


def get_auth_session(db: Session, token: str) -> AuthSessionRecord | None:
    record = db.get(AuthSessionRecord, token)
    if record:
        # expires_at is stored as naive UTC by SQLite; compare consistently.
        expires = record.expires_at
        now = utcnow().replace(tzinfo=None) if expires.tzinfo is None else utcnow()
        if expires > now:
            return record
    return None


def delete_auth_session(db: Session, token: str) -> None:
    record = db.get(AuthSessionRecord, token)
    if record:
        db.delete(record)
        db.commit()


# ── Project helpers ────────────────────────────────────────────────────────────


def create_project(db: Session, project: ProjectCreate) -> ProjectRecord:
    record = ProjectRecord(
        name=project.name,
        slug=project.slug,
        description=project.description,
        owner_id=project.owner_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_project_by_id(db: Session, project_id: int) -> ProjectRecord | None:
    return db.get(ProjectRecord, project_id)


def list_projects(db: Session, *, active_only: bool = True) -> list[ProjectRecord]:
    stmt = select(ProjectRecord).order_by(ProjectRecord.name)
    if active_only:
        stmt = stmt.where(ProjectRecord.is_active.is_(True))
    return list(db.scalars(stmt).all())


def add_project_member(
    db: Session,
    project_id: int,
    user_id: int,
    access_level: str = ProjectAccessLevel.MEMBER.value,
) -> ProjectMemberRecord:
    existing = db.scalars(
        select(ProjectMemberRecord)
        .where(ProjectMemberRecord.project_id == project_id)
        .where(ProjectMemberRecord.user_id == user_id)
    ).first()
    if existing:
        existing.access_level = access_level
        db.commit()
        return existing
    record = ProjectMemberRecord(
        project_id=project_id, user_id=user_id, access_level=access_level
    )
    db.add(record)
    db.commit()
    return record


def remove_project_member(db: Session, project_id: int, user_id: int) -> bool:
    """Remove a user from a project. Returns True if the membership existed."""
    record = db.scalars(
        select(ProjectMemberRecord)
        .where(ProjectMemberRecord.project_id == project_id)
        .where(ProjectMemberRecord.user_id == user_id)
    ).first()
    if not record:
        return False
    db.delete(record)
    db.commit()
    return True


def list_project_members(db: Session, project_id: int) -> list[ProjectMemberRecord]:
    return list(
        db.scalars(
            select(ProjectMemberRecord)
            .options(selectinload(ProjectMemberRecord.user))
            .where(ProjectMemberRecord.project_id == project_id)
            .order_by(ProjectMemberRecord.id)
        ).all()
    )


def list_users(db: Session, *, active_only: bool = True) -> list[UserRecord]:
    stmt = select(UserRecord).order_by(UserRecord.display_name)
    if active_only:
        stmt = stmt.where(UserRecord.is_active.is_(True))
    return list(db.scalars(stmt).all())


# ── Tag helpers ────────────────────────────────────────────────────────────────


def get_or_create_tag(db: Session, slug: str) -> TagRecord | None:
    return db.scalars(select(TagRecord).where(TagRecord.slug == slug)).first()


def list_tags(db: Session) -> list[TagRecord]:
    return list(db.scalars(select(TagRecord).order_by(TagRecord.name)).all())


def seed_default_tags(db: Session) -> None:
    """Idempotently insert the default impact-area tags."""
    for slug in DEFAULT_TAG_SLUGS:
        exists = db.scalars(select(TagRecord).where(TagRecord.slug == slug)).first()
        if not exists:
            db.add(
                TagRecord(
                    name=slug.capitalize(),
                    slug=slug,
                    color=TAG_COLORS.get(slug, "#64748b"),
                )
            )
    db.commit()


def link_kb_to_project(db: Session, kb_id: str, project_id: int) -> None:
    exists = db.scalars(
        select(KBProjectLinkRecord)
        .where(KBProjectLinkRecord.kb_id == kb_id)
        .where(KBProjectLinkRecord.project_id == project_id)
    ).first()
    if not exists:
        db.add(KBProjectLinkRecord(kb_id=kb_id, project_id=project_id))
        db.commit()


def get_kb_project_ids(db: Session, kb_id: str) -> list[int]:
    rows = db.scalars(
        select(KBProjectLinkRecord.project_id).where(KBProjectLinkRecord.kb_id == kb_id)
    ).all()
    return list(rows)
