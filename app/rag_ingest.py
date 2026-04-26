"""KB ingestion and Chroma storage helpers."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from langchain_chroma import Chroma
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter, MarkdownTextSplitter

from app.llm import get_embedding_model
from app.schemas import UserClearance
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_DIR = PROJECT_ROOT / "kb"
DEFAULT_CHROMA_DIR = PROJECT_ROOT / "data" / "chroma"
DEFAULT_COLLECTION_NAME = "it_helpdesk_kb"
DEFAULT_CLEARANCE_LEVEL = 0

FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

CLEARANCE_ALIASES = {
    "public": 0,
    "employee": 0,
    "internal": 1,
    "helpdesk": 1,
    "support": 1,
    "restricted": 2,
    "admin": 3,
}


@dataclass(frozen=True)
class IngestionReport:
    kb_dir: Path
    persist_directory: Path
    collection_name: str
    source_documents: int
    chunks: int
    chunk_size: int
    chunk_overlap: int


def _chroma_client_settings() -> Any | None:
    try:
        from chromadb.config import Settings as ChromaClientSettings
    except Exception:
        return None
    return ChromaClientSettings(anonymized_telemetry=False, is_persistent=True)


def get_vectorstore(
    settings: Settings | None = None,
    *,
    persist_directory: str | Path | None = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    embedding: Embeddings | None = None,
) -> Chroma:
    if settings is not None:
        persist_path = settings.chroma_dir
    else:
        persist_path = Path(persist_directory or DEFAULT_CHROMA_DIR)
    persist_path.mkdir(parents=True, exist_ok=True)
    client_settings = _chroma_client_settings()
    return Chroma(
        collection_name=collection_name,
        persist_directory=str(persist_path),
        embedding_function=embedding or get_embedding_model(settings),
        client_settings=client_settings,
        collection_metadata={"hnsw:space": "cosine"},
    )


def load_markdown_documents(kb_dir: str | Path | None = None) -> list[Document]:
    root = Path(kb_dir) if kb_dir is not None else get_settings().kb_dir
    if not root.exists():
        return []

    loader = DirectoryLoader(
        str(root),
        glob="**/*.md",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"},
        recursive=True,
        show_progress=False,
    )

    documents: list[Document] = []
    for loaded_doc in loader.load():
        source = Path(str(loaded_doc.metadata.get("source", "")))
        if source.name.startswith("."):
            continue
        try:
            relative_path = source.resolve().relative_to(root.resolve())
        except ValueError:
            relative_path = Path(source.name)

        front_matter, body = _parse_front_matter(loaded_doc.page_content)
        clean_body = body.strip()
        if not clean_body:
            continue
        metadata = _document_metadata(relative_path, front_matter, clean_body)
        documents.append(Document(page_content=clean_body, metadata=metadata))
    return documents


def split_markdown_documents(
    documents: Sequence[Document],
    *,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
) -> list[Document]:
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "heading_1"),
            ("##", "heading_2"),
            ("###", "heading_3"),
            ("####", "heading_4"),
        ],
        strip_headers=False,
    )
    chunk_splitter = MarkdownTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    header_chunks: list[Document] = []
    for document in documents:
        sections = header_splitter.split_text(document.page_content)
        for section in sections:
            section.metadata = {**document.metadata, **section.metadata}
            header_chunks.append(section)

    chunks = chunk_splitter.split_documents(header_chunks)

    source_counts: dict[str, int] = {}
    for chunk in chunks:
        metadata = dict(chunk.metadata)
        source_id = str(metadata.get("source_id", _stable_id(str(metadata.get("source", "")))))
        chunk_index = source_counts.get(source_id, 0)
        source_counts[source_id] = chunk_index + 1

        metadata["chunk_index"] = chunk_index
        metadata["chunk_id"] = _stable_id(f"{source_id}:{chunk_index}:{chunk.page_content[:80]}")
        metadata["section"] = _section_title(metadata, chunk.page_content)
        chunk.metadata = _sanitize_metadata(metadata)

    return chunks


def split_documents(documents: list[Document]) -> list[Document]:
    """Compatibility wrapper used by older call sites."""

    return split_markdown_documents(documents)


def ingest_markdown_kb(
    *,
    kb_dir: str | Path | None = None,
    persist_directory: str | Path | None = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
    reset: bool = True,
) -> IngestionReport:
    settings = get_settings()
    kb_path = Path(kb_dir) if kb_dir is not None else settings.kb_dir
    persist_path = Path(persist_directory) if persist_directory is not None else settings.chroma_dir

    source_documents = load_markdown_documents(kb_path)
    if not source_documents:
        raise FileNotFoundError(f"No Markdown documents found under {kb_path}")

    chunks = split_markdown_documents(
        source_documents,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    vectorstore = get_vectorstore(
        persist_directory=persist_path,
        collection_name=collection_name,
    )

    if reset:
        try:
            vectorstore.delete_collection()
        except Exception:
            logger.debug("Collection %s did not exist before ingest", collection_name)
        vectorstore = get_vectorstore(
            persist_directory=persist_path,
            collection_name=collection_name,
        )

    vectorstore.add_documents(chunks, ids=[str(chunk.metadata["chunk_id"]) for chunk in chunks])
    return IngestionReport(
        kb_dir=kb_path,
        persist_directory=persist_path,
        collection_name=collection_name,
        source_documents=len(source_documents),
        chunks=len(chunks),
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text

    metadata: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        metadata[_normalize_key(key)] = _parse_metadata_value(raw_value.strip())
    return metadata, text[match.end() :]


def _parse_metadata_value(value: str) -> Any:
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_metadata_value(part.strip()) for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]

    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _document_metadata(
    relative_path: Path,
    front_matter: Mapping[str, Any],
    body: str,
) -> dict[str, str | int | float | bool]:
    source = relative_path.as_posix()
    title = str(front_matter.get("title") or _extract_first_heading(body) or relative_path.stem)
    category = front_matter.get("category")
    if not category:
        category = relative_path.parts[0] if len(relative_path.parts) > 1 else "infra"

    clearance_source = front_matter.get(
        "clearance_level",
        front_matter.get("clearance", DEFAULT_CLEARANCE_LEVEL),
    )
    clearance_level = _coerce_clearance_level(clearance_source)
    metadata: dict[str, Any] = {
        "source": source,
        "source_id": _stable_id(source),
        "source_type": "kb_markdown",
        "kb_id": str(front_matter.get("kb_id") or relative_path.stem),
        "title": title,
        "category": _normalize_term(category),
        "clearance": _clearance_label(clearance_level),
        "clearance_level": clearance_level,
        "app_name": _normalize_term(front_matter.get("app_name", "general")),
        "environment": _normalize_term(front_matter.get("environment", "unknown")),
        "audience": _normalize_term(front_matter.get("audience", "all")),
        "department": _normalize_term(front_matter.get("department", "all")),
        "tags": _metadata_list(front_matter.get("tags")),
        "status": _normalize_term(front_matter.get("status", "active")),
        "updated": str(front_matter.get("updated", "")),
    }

    for key, value in front_matter.items():
        if key not in metadata and key not in {"clearance"}:
            metadata[f"fm_{key}"] = value
    return _sanitize_metadata(metadata)


def _sanitize_metadata(metadata: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    sanitized: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        safe_key = _normalize_key(str(key))
        if value is None:
            sanitized[safe_key] = ""
        elif isinstance(value, (bool, int, float, str)):
            sanitized[safe_key] = value
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            sanitized[safe_key] = ", ".join(str(item) for item in value)
        else:
            sanitized[safe_key] = str(value)
    return sanitized


def _metadata_list(value: Any) -> str:
    return ", ".join(_normalize_many(value))


def _normalize_many(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        if "," in value:
            return [_normalize_term(part) for part in value.split(",") if part.strip()]
        return [_normalize_term(value)]
    if isinstance(value, Sequence):
        return [_normalize_term(item) for item in value if str(item).strip()]
    return [_normalize_term(value)]


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")


def _normalize_term(value: Any) -> str:
    raw_value = value.value if hasattr(value, "value") else value
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(raw_value).strip().lower())
    return normalized.strip("-") or "all"


def _coerce_clearance_level(value: Any) -> int:
    raw_value = value.value if hasattr(value, "value") else value
    if isinstance(raw_value, bool):
        return int(raw_value)
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, float):
        return int(raw_value)
    text = str(raw_value).strip().lower()
    if text in CLEARANCE_ALIASES:
        return CLEARANCE_ALIASES[text]
    try:
        return int(text)
    except ValueError:
        logger.warning("Unknown clearance level %r; defaulting to public", value)
        return DEFAULT_CLEARANCE_LEVEL


def _clearance_label(level: int) -> str:
    if level <= 0:
        return UserClearance.PUBLIC.value
    if level == 1:
        return UserClearance.INTERNAL.value
    return UserClearance.RESTRICTED.value


def _extract_first_heading(markdown: str) -> str | None:
    match = HEADING_RE.search(markdown)
    return match.group(2).strip() if match else None


def _section_title(metadata: Mapping[str, Any], content: str) -> str:
    for key in ("heading_4", "heading_3", "heading_2", "heading_1"):
        value = metadata.get(key)
        if value:
            return str(value)
    return _extract_first_heading(content) or str(metadata.get("title", "Untitled"))


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
