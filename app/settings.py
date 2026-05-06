import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="Capstone ITS v2", alias="APP_NAME")
    environment: str = Field(default="local", alias="ENVIRONMENT", min_length=1)

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    nvidia_api_key: str | None = Field(default=None, alias="NVIDIA_API_KEY")
    nvidia_model: str = Field(
        default="stepfun-ai/step-3.5-flash",
        alias="NVIDIA_MODEL",
    )
    nvidia_embedding_model: str = Field(
        default="nvidia/nv-embedqa-e5-v5",
        alias="NVIDIA_EMBEDDING_MODEL",
    )
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    groq_model: str = Field(
        default="llama-3.3-70b-versatile",
        alias="GROQ_MODEL",
    )
    openai_embedding_model: str = Field(
        default="text-embedding-3-small",
        alias="OPENAI_EMBEDDING_MODEL",
    )
    openai_embedding_dimensions: int | None = Field(
        default=None,
        alias="OPENAI_EMBEDDING_DIMENSIONS",
        ge=1,
    )
    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")
    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE", ge=0.0, le=2.0)
    ollama_model: str = Field(default="llama3.1", alias="OLLAMA_MODEL")
    ollama_base_url: str = Field(
        default="http://localhost:11434", alias="OLLAMA_BASE_URL"
    )
    embedding_provider: str = Field(default="openai", alias="EMBEDDING_PROVIDER")
    huggingface_embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        alias="HUGGINGFACE_EMBEDDING_MODEL",
    )
    pinecone_api_key: str | None = Field(default=None, alias="PINECONE_API_KEY")
    pinecone_kb_index_name: str = Field(
        default="its-knowledge-base",
        alias="PINECONE_KB_INDEX_NAME",
    )
    pinecone_db_index_name: str = Field(
        default="its-db-schema",
        alias="PINECONE_DB_INDEX_NAME",
    )
    pinecone_ticket_index_name: str = Field(
        default="its-tickets",
        alias="PINECONE_TICKET_INDEX_NAME",
    )
    pinecone_comment_index_name: str = Field(
        default="its-comments",
        alias="PINECONE_COMMENT_INDEX_NAME",
    )
    database_url: str = Field(
        default="sqlite:///./data/helpdesk.db", alias="DATABASE_URL"
    )
    sqlite_connect_timeout: float = Field(
        default=30.0, alias="SQLITE_CONNECT_TIMEOUT", gt=0
    )
    sql_echo: bool = Field(default=False, alias="SQL_ECHO")
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        alias="CORS_ALLOWED_ORIGINS",
    )

    kb_dir: Path = Field(default=Path("./kb"), alias="KB_DIR")
    max_requirement_turns: int = Field(
        default=3, alias="MAX_REQUIREMENT_TURNS", ge=1, le=10
    )
    standard_user_clearance: str = Field(
        default="public", alias="STANDARD_USER_CLEARANCE"
    )
    langgraph_checkpoint_path: Path = Field(
        default=Path("./data/langgraph_checkpoints.sqlite"),
        alias="LANGGRAPH_CHECKPOINT_PATH",
    )
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: str | None = Field(default=None, alias="LANGSMITH_API_KEY")
    langsmith_project: str | None = Field(default=None, alias="LANGSMITH_PROJECT")
    langsmith_endpoint: str | None = Field(default=None, alias="LANGSMITH_ENDPOINT")
    langsmith_workspace_id: str | None = Field(
        default=None, alias="LANGSMITH_WORKSPACE_ID"
    )

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL cannot be empty")
        if "://" not in value:
            raise ValueError("DATABASE_URL must be a SQLAlchemy URL")
        return value

    @field_validator("openai_embedding_dimensions", mode="before")
    @classmethod
    def blank_embedding_dimensions_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_allowed_origins(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            if not value.strip():
                return []
            # Accept comma-separated origins for simple .env usage.
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            normalized: list[str] = []
            for origin in value:
                if isinstance(origin, str) and origin.strip():
                    normalized.append(origin.strip())
            return normalized
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sqlite_path(self) -> Path | None:
        if not self.is_sqlite:
            return None
        if self.database_url in {
            "sqlite://",
            "sqlite:///:memory:",
            "sqlite+pysqlite:///:memory:",
        }:
            return None

        prefix = "sqlite:///"
        if self.database_url.startswith("sqlite+pysqlite:///"):
            prefix = "sqlite+pysqlite:///"
        if not self.database_url.startswith(prefix):
            return None

        return Path(self.database_url.removeprefix(prefix))

    def ensure_local_dirs(self) -> None:
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    def configure_langsmith_environment(self) -> None:
        if not self.langsmith_tracing:
            return

        os.environ["LANGSMITH_TRACING"] = "true"
        # Backward-compatible alias used by some LangChain/LangGraph integrations.
        os.environ["LANGCHAIN_TRACING_V2"] = "true"

        if self.langsmith_api_key:
            os.environ["LANGSMITH_API_KEY"] = self.langsmith_api_key
        if self.langsmith_project:
            os.environ["LANGSMITH_PROJECT"] = self.langsmith_project
        if self.langsmith_endpoint:
            os.environ["LANGSMITH_ENDPOINT"] = self.langsmith_endpoint
        if self.langsmith_workspace_id:
            os.environ["LANGSMITH_WORKSPACE_ID"] = self.langsmith_workspace_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
