from __future__ import annotations

import logging
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    KBArticleRef,
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
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    del connection_record
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ── Auth models ───────────────────────────────────────────────────────────────

class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(40), default=UserRole.USER.value, index=True)
    clearance: Mapped[str] = mapped_column(String(40), default=UserClearance.PUBLIC.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project_memberships: Mapped[list[ProjectMemberRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list[AuthSessionRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSessionRecord(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # opaque token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str] = mapped_column(String(512), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")

    user: Mapped[UserRecord] = relationship(back_populates="sessions")


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    thread_id: Mapped[str] = mapped_column(String(120), index=True)
    role: Mapped[str] = mapped_column(String(40), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
    )


# ── Project models ─────────────────────────────────────────────────────────────

class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    members: Mapped[list[ProjectMemberRecord]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    kb_links: Mapped[list[KBProjectLinkRecord]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMemberRecord(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    access_level: Mapped[str] = mapped_column(String(40), default=ProjectAccessLevel.MEMBER.value)

    project: Mapped[ProjectRecord] = relationship(back_populates="members")
    user: Mapped[UserRecord] = relationship(back_populates="project_memberships")


# ── Tag models ─────────────────────────────────────────────────────────────────

class TagRecord(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False)
    color: Mapped[str] = mapped_column(String(20), default="#64748b")
    description: Mapped[str] = mapped_column(String(500), default="")

    ticket_links: Mapped[list[TicketTagRecord]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )
    kb_links: Mapped[list[KBTagRecord]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )


class TicketTagRecord(Base):
    __tablename__ = "ticket_tags"
    __table_args__ = (UniqueConstraint("ticket_id", "tag_id", name="uq_ticket_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)

    ticket: Mapped[TicketRecord] = relationship(back_populates="tag_links")
    tag: Mapped[TagRecord] = relationship(back_populates="ticket_links")


class KBTagRecord(Base):
    __tablename__ = "kb_tags"
    __table_args__ = (UniqueConstraint("kb_id", "tag_id", name="uq_kb_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(255), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)

    tag: Mapped[TagRecord] = relationship(back_populates="kb_links")


class KBProjectLinkRecord(Base):
    __tablename__ = "kb_project_links"
    __table_args__ = (UniqueConstraint("kb_id", "project_id", name="uq_kb_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )

    project: Mapped[ProjectRecord] = relationship(back_populates="kb_links")


# ── Ticket model ───────────────────────────────────────────────────────────────

class TicketRecord(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default=TicketStatus.OPEN.value, index=True)
    user_id: Mapped[str] = mapped_column(String(120), default="anonymous", index=True)
    thread_id: Mapped[str] = mapped_column(String(120), index=True)
    app_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    environment: Mapped[str] = mapped_column(
        String(40),
        default=Environment.UNKNOWN.value,
        index=True,
    )
    user_clearance: Mapped[str] = mapped_column(String(40), default=UserClearance.PUBLIC.value)

    category: Mapped[str] = mapped_column(
        String(128),
        default=TicketCategory.INFRA.value,
        index=True,
    )
    suggested_priority: Mapped[str] = mapped_column(
        String(40),
        default=Priority.MEDIUM.value,
        index=True,
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)

    intelligence: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    resolution: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    guardrail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    conversation: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
    )
    raw_context: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)

    # Project association
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Legacy column retained so the generated SQLite DB from earlier scaffolding remains usable.
    sentiment: Mapped[str] = mapped_column(String(40), default="Calm")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    messages: Mapped[list[TicketMessageRecord]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketMessageRecord.created_at",
    )
    kb_links: Mapped[list[TicketKBLinkRecord]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
    )
    related_ticket_links: Mapped[list[RelatedTicketLinkRecord]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        foreign_keys="RelatedTicketLinkRecord.ticket_id",
    )
    tag_links: Mapped[list[TicketTagRecord]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
    )


class TicketMessageRecord(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
    )

    ticket: Mapped[TicketRecord] = relationship(back_populates="messages")


class TicketKBLinkRecord(Base):
    __tablename__ = "ticket_kb_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    kb_id: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(500))
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    clearance: Mapped[str] = mapped_column(String(40), default=UserClearance.PUBLIC.value)

    ticket: Mapped[TicketRecord] = relationship(back_populates="kb_links")


class RelatedTicketLinkRecord(Base):
    __tablename__ = "duplicate_ticket_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    duplicate_ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)

    ticket: Mapped[TicketRecord] = relationship(
        foreign_keys=[ticket_id],
        back_populates="related_ticket_links",
    )
    duplicate_ticket: Mapped[TicketRecord] = relationship(foreign_keys=[duplicate_ticket_id])


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
    return [ChatMessage.model_validate(message) for message in record.conversation or []]


def _ticket_to_read(record: TicketRecord) -> TicketRead:
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
        resolution=ResolutionData.model_validate(record.resolution or {}),
        conversation=_conversation_for_record(record),
        guardrail=GuardrailDecision.model_validate(record.guardrail) if record.guardrail else None,
        raw_context=record.raw_context or {},
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def create_ticket(db: Session, ticket: TicketCreate, *, index_vector: bool = True) -> TicketRead:
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
    resolution = ticket.resolution.model_dump(mode="json")
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
        guardrail=ticket.guardrail.model_dump(mode="json") if ticket.guardrail else None,
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

    for article in ticket.resolution.linked_kb_articles:
        db.add(
            TicketKBLinkRecord(
                ticket_id=record.id,
                kb_id=article.kb_id,
                title=article.title,
                source=article.source,
                relevance_score=article.relevance_score,
                clearance=article.clearance.value,
            )
        )

    for duplicate_id in ticket.resolution.duplicate_ticket_ids:
        db.add(
            RelatedTicketLinkRecord(
                ticket_id=record.id,
                duplicate_ticket_id=duplicate_id,
                score=1.0,
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
    records = db.scalars(
        select(TicketRecord)
        .options(*_ticket_read_options())
        .where(TicketRecord.id.in_(ticket_ids))
    ).unique().all()
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
) -> list[dict[str, Any]]:
    """Semantic ticket search with keyword fallback.

    Pinecone handles content-level recall when the ticket vector index is
    configured. SQL keyword search remains the zero-dependency local fallback.
    """
    if not query or not query.strip():
        return []

    vector_results = _search_ticket_vectors(
        query,
        user_id=user_id,
        project_id=project_id,
        tag_slugs=tag_slugs,
        status=status,
        priority=priority,
        limit=limit,
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
                    "user_id": record.user_id,
                    "project_id": record.project_id,
                    "tags": _tag_slugs_for_record(record),
                    "created_at": record.created_at.isoformat() if record.created_at else None,
                    "score": hits,
                    "source": "sql",
                }
            )

    results.sort(key=lambda r: r["score"], reverse=True)
    if not vector_results:
        return results[:limit]

    seen = {result["ticket_id"] for result in vector_results}
    merged = [
        *vector_results,
        *(result for result in results if result["ticket_id"] not in seen),
    ]
    return merged[:limit]


def find_duplicate_candidates(
    db: Session,
    keywords: list[str],
    exclude_ticket_id: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if not keywords:
        return []

    try:
        vector_results = _search_ticket_vectors(
            " ".join(keywords),
            exclude_ticket_id=exclude_ticket_id,
            limit=limit,
        )
    except TicketVectorUnavailableError:
        logger.warning("Skipping duplicate vector search because the ticket vector store failed")
        vector_results = []

    wanted = {keyword.casefold() for keyword in keywords}
    duplicate_conditions = _ticket_match_conditions(list(wanted))
    stmt = (
        select(TicketRecord)
        .where(or_(*duplicate_conditions))
        .order_by(TicketRecord.created_at.desc())
        .limit(max(limit * 10, 50))
    )
    if exclude_ticket_id is not None:
        stmt = stmt.where(TicketRecord.id != exclude_ticket_id)

    matches: list[dict[str, Any]] = []
    for record in db.scalars(stmt).all():
        existing = {keyword.casefold() for keyword in (record.keywords or [])}
        if not existing:
            continue
        score = len(wanted & existing) / max(len(wanted | existing), 1)
        if score > 0:
            matches.append({"ticket_id": record.id, "summary": record.summary, "score": score})

    matches.sort(key=lambda item: item["score"], reverse=True)
    vector_matches = [
        {
            "ticket_id": result["ticket_id"],
            "summary": result["summary"],
            "score": result["score"],
        }
        for result in vector_results
    ]
    if not vector_matches:
        return matches[:limit]

    seen = {result["ticket_id"] for result in vector_matches}
    merged = [
        *vector_matches,
        *(result for result in matches if result["ticket_id"] not in seen),
    ]
    return merged[:limit]


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
        logger.warning("Ticket vector indexing failed for ticket %s: %s", ticket.id, exc)


def _index_ticket_vectors(tickets: Sequence[TicketRead], *, batch_size: int = 100) -> None:
    if not tickets:
        return
    try:
        from app.ticket_vector import index_tickets

        index_tickets(tickets, reset=False, batch_size=batch_size)
    except Exception as exc:
        logger.warning("Ticket vector batch indexing failed for %s tickets: %s", len(tickets), exc)


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
        raise TicketVectorUnavailableError("Ticket vector search dependency unavailable") from exc

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


def kb_refs_from_records(records: list[TicketKBLinkRecord]) -> list[KBArticleRef]:
    return [
        KBArticleRef(
            kb_id=record.kb_id,
            title=record.title,
            source=record.source,
            relevance_score=record.relevance_score,
            clearance=UserClearance(record.clearance),
        )
        for record in records
    ]


# ── User helpers ───────────────────────────────────────────────────────────────

def get_user_by_email(db: Session, email: str) -> UserRecord | None:
    return db.scalars(select(UserRecord).where(UserRecord.email == email.casefold())).first()


def get_user_by_id(db: Session, user_id: int) -> UserRecord | None:
    return db.get(UserRecord, user_id)


def create_user(db: Session, email: str, display_name: str, hashed_password: str,
                role: str = UserRole.USER.value,
                clearance: str = UserClearance.PUBLIC.value) -> UserRecord:
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

def create_auth_session(db: Session, user_id: int, token: str, expires_at: datetime,
                        user_agent: str = "", ip_address: str = "") -> AuthSessionRecord:
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


def add_project_member(db: Session, project_id: int, user_id: int,
                       access_level: str = ProjectAccessLevel.MEMBER.value) -> ProjectMemberRecord:
    existing = db.scalars(
        select(ProjectMemberRecord)
        .where(ProjectMemberRecord.project_id == project_id)
        .where(ProjectMemberRecord.user_id == user_id)
    ).first()
    if existing:
        existing.access_level = access_level
        db.commit()
        return existing
    record = ProjectMemberRecord(project_id=project_id, user_id=user_id, access_level=access_level)
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
            db.add(TagRecord(
                name=slug.capitalize(),
                slug=slug,
                color=TAG_COLORS.get(slug, "#64748b"),
            ))
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
