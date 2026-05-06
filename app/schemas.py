from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class AppModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class TicketCategory(StrEnum):
    BUG = "Bug"
    FEATURE = "Feature"
    UI = "UI"
    INFRA = "Infra"
    HARDWARE = "Hardware"


class TicketStatus(StrEnum):
    OPEN = "Open"
    TRIAGED = "Triaged"
    IN_PROGRESS = "In Progress"
    RESOLVED = "Resolved"
    CLOSED = "Closed"


class Priority(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class Environment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    UNKNOWN = "unknown"


class UserClearance(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessage(AppModel):
    role: MessageRole
    content: str = Field(min_length=1, max_length=8_000)
    created_at: datetime = Field(default_factory=utcnow)


class RequirementAssessment(AppModel):
    is_clear: bool
    normalized_problem: str = Field(default="", max_length=1_200)
    missing_fields: list[str] = Field(default_factory=list, max_length=6)
    follow_up_question: str | None = Field(default=None, max_length=500)
    confidence: Confidence

    @field_validator("follow_up_question")
    @classmethod
    def blank_follow_up_to_none(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value


class IssueClassification(AppModel):
    category: TicketCategory
    confidence: Confidence
    reasoning: str = Field(min_length=1, max_length=1_000)


class GuardrailDecision(AppModel):
    is_safe: bool
    risk_level: Priority
    authorization_required: bool
    can_self_resolve: bool
    reason: str = Field(min_length=1, max_length=1_000)
    detected_patterns: list[str] = Field(default_factory=list, max_length=10)


class KBArticleRef(AppModel):
    kb_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=500)
    relevance_score: Confidence = 0.0
    clearance: UserClearance = UserClearance.PUBLIC


class SelfResolutionAnswer(AppModel):
    answer: str = Field(min_length=1, max_length=3_000)
    confidence: Confidence
    linked_kb_articles: list[KBArticleRef] = Field(default_factory=list, max_length=5)
    should_escalate: bool
    escalation_reason: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def require_escalation_reason(self) -> SelfResolutionAnswer:
        if self.should_escalate and not self.escalation_reason:
            raise ValueError(
                "escalation_reason is required when should_escalate is true"
            )
        return self


class AnswerValidation(AppModel):
    is_safe: bool
    contains_pii: bool
    contains_secrets: bool
    sanitized_answer: str = Field(min_length=1, max_length=3_000)
    reason: str = Field(min_length=1, max_length=1_000)


class TicketIntelligence(AppModel):
    category: TicketCategory
    suggested_priority: Priority
    summary: str = Field(min_length=1, max_length=1_200)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    confidence: Confidence


class ResolutionData(AppModel):
    linked_kb_articles: list[KBArticleRef] = Field(default_factory=list, max_length=10)
    duplicate_ticket_ids: list[int] = Field(default_factory=list, max_length=20)
    suggested_fixes: list[str] = Field(default_factory=list, max_length=10)


# ── Auth & identity ────────────────────────────────────────────────────────────


class UserRole(StrEnum):
    USER = "user"
    AGENT = "agent"
    ADMIN = "admin"


class ProjectAccessLevel(StrEnum):
    VIEWER = "viewer"
    MEMBER = "member"
    OWNER = "owner"


class UserCreate(AppModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.USER
    clearance: UserClearance = UserClearance.PUBLIC


class UserRead(AppModel):
    id: int
    email: str
    display_name: str
    role: UserRole
    clearance: UserClearance
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class SessionCreate(AppModel):
    user_id: int
    user_agent: str = Field(default="", max_length=512)
    ip_address: str = Field(default="", max_length=64)


class SessionRead(AppModel):
    id: str
    user_id: int
    expires_at: datetime
    created_at: datetime


# ── Projects ───────────────────────────────────────────────────────────────────


class ProjectCreate(AppModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    description: str = Field(default="", max_length=1_000)
    owner_id: int


class ProjectRead(AppModel):
    id: int
    name: str
    slug: str
    description: str
    owner_id: int
    is_active: bool
    created_at: datetime


class ProjectMemberAdd(AppModel):
    project_id: int
    user_id: int
    access_level: ProjectAccessLevel = ProjectAccessLevel.MEMBER


# ── Tags ───────────────────────────────────────────────────────────────────────

DEFAULT_TAG_SLUGS = [
    "ui",
    "hardware",
    "access",
    "infra",
    "security",
    "network",
    "performance",
    "data",
]

TAG_COLORS: dict[str, str] = {
    "ui": "#7c3aed",
    "hardware": "#b45309",
    "access": "#0369a1",
    "infra": "#0f766e",
    "security": "#b91c1c",
    "network": "#1d4ed8",
    "performance": "#c2410c",
    "data": "#15803d",
}


class TagCreate(AppModel):
    name: str = Field(min_length=1, max_length=60)
    slug: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")
    color: str = Field(default="#64748b", max_length=20)
    description: str = Field(default="", max_length=500)


class TagRead(AppModel):
    id: int
    name: str
    slug: str
    color: str
    description: str


# ── Updated ticket models ──────────────────────────────────────────────────────


class TicketCreate(AppModel):
    user_id: str = Field(min_length=1, max_length=120)
    thread_id: str = Field(min_length=1, max_length=120)
    status: TicketStatus = TicketStatus.OPEN
    app_name: str | None = Field(default=None, max_length=120)
    environment: Environment = Environment.UNKNOWN
    user_clearance: UserClearance = UserClearance.PUBLIC
    project_id: int | None = None
    tag_slugs: list[str] = Field(default_factory=list, max_length=10)
    intelligence: TicketIntelligence
    resolution: ResolutionData = Field(default_factory=ResolutionData)
    conversation: list[ChatMessage] = Field(default_factory=list, max_length=200)
    guardrail: GuardrailDecision | None = None
    raw_context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TicketRead(AppModel):
    id: int
    user_id: str
    thread_id: str
    status: TicketStatus
    app_name: str | None
    environment: Environment
    user_clearance: UserClearance
    project_id: int | None = None
    tag_slugs: list[str] = Field(default_factory=list)
    intelligence: TicketIntelligence
    resolution: ResolutionData
    conversation: list[ChatMessage]
    guardrail: GuardrailDecision | None
    raw_context: dict[str, Any]
    created_at: datetime
    updated_at: datetime


CommentTag = Literal[
    "detail",
    "status_update",
    "rca_clue",
    "blocker",
    "ticket_ref",
    "noise",
]


class CommentClassification(AppModel):
    tags: list[CommentTag] = Field(default_factory=list, max_length=3)
    references_tickets: list[str] = Field(default_factory=list, max_length=20)
    references_systems: list[str] = Field(default_factory=list, max_length=20)
    references_people: list[str] = Field(default_factory=list, max_length=20)
    signal_strength: Confidence = 0.0
    summary: str = Field(default="", max_length=300)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> object:
        allowed = {
            "detail",
            "status_update",
            "rca_clue",
            "blocker",
            "ticket_ref",
            "noise",
        }
        if value is None:
            return ["detail"]
        source = value if isinstance(value, list) else [value]
        tags: list[str] = []
        seen: set[str] = set()
        for item in source:
            tag = str(item).strip().casefold()
            if tag not in allowed or tag in seen:
                continue
            seen.add(tag)
            tags.append(tag)
            if len(tags) >= 3:
                break
        return tags or ["detail"]

    @field_validator(
        "references_tickets",
        "references_systems",
        "references_people",
        mode="before",
    )
    @classmethod
    def normalize_reference_values(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = value.strip()
            return [cleaned] if cleaned else []
        if isinstance(value, list):
            deduped: list[str] = []
            seen: set[str] = set()
            for item in value:
                text = str(item).strip()
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(text)
            return deduped
        text = str(value).strip()
        return [text] if text else []


class CommentVectorMetadata(CommentClassification):
    comment_id: str = Field(min_length=3, max_length=32, pattern=r"^C-\d+$")
    ticket_id: str = Field(min_length=3, max_length=32, pattern=r"^T-\d+$")


class CommentMetadataFilters(AppModel):
    ticket_ids: list[int] = Field(default_factory=list, max_length=50)
    comment_ids: list[int] = Field(default_factory=list, max_length=50)
    tags: list[CommentTag] = Field(default_factory=list, max_length=6)
    references_tickets: list[str] = Field(default_factory=list, max_length=30)
    references_systems: list[str] = Field(default_factory=list, max_length=30)
    references_people: list[str] = Field(default_factory=list, max_length=30)
    author_user_ids: list[int] = Field(default_factory=list, max_length=50)
    parent_comment_ids: list[int] = Field(default_factory=list, max_length=50)
    min_signal_strength: Confidence | None = None
    max_signal_strength: Confidence | None = None

    @field_validator(
        "references_tickets",
        "references_systems",
        "references_people",
        mode="before",
    )
    @classmethod
    def normalize_filter_reference_values(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = value.strip()
            return [cleaned] if cleaned else []
        if isinstance(value, list):
            deduped: list[str] = []
            seen: set[str] = set()
            for item in value:
                text = str(item).strip()
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(text)
            return deduped
        text = str(value).strip()
        return [text] if text else []

    @model_validator(mode="after")
    def validate_signal_range(self) -> CommentMetadataFilters:
        if (
            self.min_signal_strength is not None
            and self.max_signal_strength is not None
            and self.min_signal_strength > self.max_signal_strength
        ):
            raise ValueError(
                "min_signal_strength must be less than or equal to max_signal_strength"
            )
        return self


_COMMENT_FILTER_KEYS = {
    "ticket_ids",
    "comment_ids",
    "tags",
    "references_tickets",
    "references_systems",
    "references_people",
    "author_user_ids",
    "parent_comment_ids",
    "min_signal_strength",
    "max_signal_strength",
}


class TicketCommentRetrieverInput(AppModel):
    query: str = Field(min_length=1, max_length=1200)
    metadata_filters: CommentMetadataFilters | None = None
    limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="before")
    @classmethod
    def hoist_filter_keys(cls, values: object) -> object:
        """Allow LLMs to pass CommentMetadataFilters fields at the top level.

        The LLM sometimes sends e.g. ``ticket_ids=[1, 2]`` directly instead of
        nesting it under ``metadata_filters``.  This validator moves any such
        keys into ``metadata_filters`` so the call still validates correctly.
        """
        if not isinstance(values, dict):
            return values
        extra = {k: v for k, v in values.items() if k in _COMMENT_FILTER_KEYS}
        if not extra:
            return values
        values = {k: v for k, v in values.items() if k not in _COMMENT_FILTER_KEYS}
        existing = values.get("metadata_filters")
        if isinstance(existing, dict):
            merged = {**extra, **existing}  # explicit metadata_filters wins
        elif existing is None:
            merged = extra
        else:
            return {**values, **{"metadata_filters": existing}}
        values["metadata_filters"] = merged
        return values

    @field_validator("metadata_filters", mode="before")
    @classmethod
    def coerce_metadata_filters(cls, value: object) -> object:
        if value is None or isinstance(value, dict):
            return value
        if isinstance(value, str):
            import json
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            return None
        return value


class ChatTurnResult(AppModel):
    thread_id: str = Field(min_length=1, max_length=120)
    response: str = Field(min_length=1, max_length=4_000)
    route: Literal["follow_up", "self_resolution", "ticket_created", "blocked"]
    ticket_id: int | None = None
    linked_kb_articles: list[KBArticleRef] = Field(default_factory=list, max_length=10)
    agent_response: AgentResponse | None = None


def append_chat_messages(
    existing: list[ChatMessage] | None,
    new: list[ChatMessage] | None,
) -> list[ChatMessage]:
    return [*(existing or []), *(new or [])]


class HelpdeskGraphState(TypedDict, total=False):
    user_id: str
    thread_id: str
    app_name: str | None
    environment: Environment
    user_clearance: UserClearance
    messages: Annotated[list[ChatMessage], append_chat_messages]
    turn_count: int
    requirement: RequirementAssessment
    classification: IssueClassification
    guardrail: GuardrailDecision
    rag_answer: SelfResolutionAnswer
    answer_validation: AnswerValidation
    ticket: TicketRead
    final_response: ChatTurnResult
    route: str


# ── Chart / structured agent response ─────────────────────────────────────────


class ChartConfiguration(BaseModel):
    """Describes a chart to be rendered by the frontend."""

    chart_type: Literal["line", "bar"]
    title: str
    data: list[dict[str, Any]]
    x_axis_key: str
    data_keys: list[str]
    colors: list[str] | None = None


class AgentResponse(BaseModel):
    """Top-level structured response returned by the analytics agent."""

    markdown_text: str | None = None
    chart: ChartConfiguration | None = None
