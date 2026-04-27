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
            raise ValueError("escalation_reason is required when should_escalate is true")
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
    "ui", "hardware", "access", "infra", "security",
    "network", "performance", "data",
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


class ChatTurnResult(AppModel):
    thread_id: str = Field(min_length=1, max_length=120)
    response: str = Field(min_length=1, max_length=4_000)
    route: Literal["follow_up", "self_resolution", "ticket_created", "blocked"]
    ticket_id: int | None = None
    linked_kb_articles: list[KBArticleRef] = Field(default_factory=list, max_length=10)


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
