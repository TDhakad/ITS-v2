from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.settings import Settings, get_settings

load_dotenv()


def get_chat_model(settings: Settings | None = None) -> BaseChatModel:
    settings = settings or get_settings()
    settings.configure_langsmith_environment()
    provider = settings.llm_provider.casefold()

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai.")
        return ChatOpenAI(
            model=settings.openai_model,
            temperature=settings.llm_temperature,
            api_key=settings.openai_api_key,
        )

    if provider == "ollama":
        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=settings.llm_temperature,
        )

    if provider in {"nvidia", "nvidia-endpoints", "nvidia_ai_endpoints"}:
        if not settings.nvidia_api_key:
            raise RuntimeError("NVIDIA_API_KEY is required when LLM_PROVIDER=nvidia.")

        try:
            from langchain_nvidia_ai_endpoints import ChatNVIDIA
        except Exception as exc:
            raise RuntimeError(
                "langchain-nvidia-ai-endpoints is required when LLM_PROVIDER=nvidia. "
                "Install dependencies with uv sync."
            ) from exc

        try:
            return ChatNVIDIA(
                model=settings.nvidia_model,
                temperature=settings.llm_temperature,
                api_key=settings.nvidia_api_key,
                #  base_url='https://integrate.api.nvidia.com/v1'
            )
        except TypeError:
            return ChatNVIDIA(
                model=settings.nvidia_model,
                temperature=settings.llm_temperature,
                nvidia_api_key=settings.nvidia_api_key,
            )

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is required when LLM_PROVIDER=groq.")
        return ChatGroq(
            model=settings.groq_model,
            temperature=settings.llm_temperature,
            api_key=settings.groq_api_key,
        )

    raise ValueError(f"Unsupported LLM_PROVIDER={settings.llm_provider!r}.")


@lru_cache(maxsize=4)
def _cached_huggingface_embeddings(model_name: str) -> HuggingFaceEmbeddings:
    """Load a SentenceTransformer embedding model once and reuse it."""
    return HuggingFaceEmbeddings(model_name=model_name)


def get_embedding_model(settings: Settings | None = None) -> Embeddings:
    settings = settings or get_settings()
    settings.configure_langsmith_environment()
    provider = settings.embedding_provider.casefold()

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai."
            )
        kwargs = {
            "model": settings.openai_embedding_model,
            "api_key": settings.openai_api_key,
        }
        if settings.openai_embedding_dimensions is not None:
            kwargs["dimensions"] = settings.openai_embedding_dimensions
        return OpenAIEmbeddings(
            **kwargs,
        )

    if provider in {"huggingface", "hf"}:
        return _cached_huggingface_embeddings(settings.huggingface_embedding_model)

    if provider in {"nvidia", "nvidia-endpoints", "nvidia_ai_endpoints"}:
        if not settings.nvidia_api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY is required when EMBEDDING_PROVIDER=nvidia."
            )

        try:
            from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
        except Exception as exc:
            raise RuntimeError(
                "langchain-nvidia-ai-endpoints is required when EMBEDDING_PROVIDER=nvidia. "
                "Install dependencies with uv sync."
            ) from exc

        try:
            return NVIDIAEmbeddings(
                model=settings.nvidia_embedding_model,
                api_key=settings.nvidia_api_key,
            )
        except TypeError:
            return NVIDIAEmbeddings(
                model=settings.nvidia_embedding_model,
                nvidia_api_key=settings.nvidia_api_key,
            )

    raise ValueError(f"Unsupported EMBEDDING_PROVIDER={settings.embedding_provider!r}.")
