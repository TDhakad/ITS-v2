from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="Capstone ITS v2", alias="APP_NAME")
    environment: str = Field(default="local", alias="ENVIRONMENT", min_length=1)

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    openai_embedding_model: str = Field(
        default="text-embedding-3-small",
        alias="OPENAI_EMBEDDING_MODEL",
    )
    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")
    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE", ge=0.0, le=2.0)
    ollama_model: str = Field(default="llama3.1", alias="OLLAMA_MODEL")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
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
    pinecone_ticket_index_name: str = Field(
        default="its-tickets",
        alias="PINECONE_TICKET_INDEX_NAME",
    )
    database_url: str = Field(default="sqlite:///./data/helpdesk.db", alias="DATABASE_URL")
    sqlite_connect_timeout: float = Field(default=30.0, alias="SQLITE_CONNECT_TIMEOUT", gt=0)
    sql_echo: bool = Field(default=False, alias="SQL_ECHO")

    kb_dir: Path = Field(default=Path("./kb"), alias="KB_DIR")
    max_requirement_turns: int = Field(default=3, alias="MAX_REQUIREMENT_TURNS", ge=1, le=10)
    standard_user_clearance: str = Field(default="public", alias="STANDARD_USER_CLEARANCE")
    langgraph_checkpoint_path: Path = Field(
        default=Path("./data/langgraph_checkpoints.sqlite"),
        alias="LANGGRAPH_CHECKPOINT_PATH",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL cannot be empty")
        if "://" not in value:
            raise ValueError("DATABASE_URL must be a SQLAlchemy URL")
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sqlite_path(self) -> Path | None:
        if not self.is_sqlite:
            return None
        if self.database_url in {"sqlite://", "sqlite:///:memory:", "sqlite+pysqlite:///:memory:"}:
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
