"""KB retrieval and ranking helpers."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_community.document_compressors import FlashrankRerank
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.rag_ingest import (
    DEFAULT_CLEARANCE_LEVEL,
    DEFAULT_COLLECTION_NAME,
    _clearance_label,
    _coerce_clearance_level,
    _normalize_many,
    _normalize_term,
    _stable_id,
    get_vectorstore,
    ingest_markdown_kb,
)
from app.schemas import Environment, KBArticleRef, TicketCategory, UserClearance
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./:-]*", re.IGNORECASE)

CATEGORY_ALIASES = {
    "infra": ["infra", "network", "accounts", "security", "vpn", "identity"],
}


@dataclass(frozen=True)
class RetrievalContext:
    category: TicketCategory | str | None = None
    app_name: str | None = None
    environment: str | None = None
    user_clearance: UserClearance | str = UserClearance.PUBLIC


@dataclass
class RetrievalResult:
    document: Document
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None
    reranker: str | None = None

    @property
    def source(self) -> str:
        return str(self.document.metadata.get("source", "unknown"))

    @property
    def title(self) -> str:
        return str(self.document.metadata.get("title", self.source))


@dataclass
class _Candidate:
    document: Document
    score: float = 0.0
    vector_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


class KnowledgeBaseRAG:
    """Hybrid Chroma vector plus BM25 keyword retriever."""

    def __init__(
        self,
        *,
        persist_directory: str | Path | None = None,
        collection_name: str = DEFAULT_COLLECTION_NAME,
        embedding: Embeddings | None = None,
        reranker: FlashrankRerank | None = None,
    ) -> None:
        self.persist_directory = Path(persist_directory or get_settings().chroma_dir)
        self.collection_name = collection_name
        self.vectorstore = get_vectorstore(
            persist_directory=self.persist_directory,
            collection_name=collection_name,
            embedding=embedding,
        )
        self.reranker = reranker

    def retrieve(
        self,
        query: str,
        *,
        user_context: RetrievalContext | Mapping[str, Any] | Any | None = None,
        category: TicketCategory | str | None = None,
        clearance_level: int | str | UserClearance | None = None,
        k: int = 5,
        vector_k: int | None = None,
        keyword_k: int | None = None,
        rerank: bool = True,
        rerank_top_n: int | None = None,
        vector_weight: float = 0.55,
        keyword_weight: float = 0.45,
    ) -> list[RetrievalResult]:
        clean_query = query.strip()
        if not clean_query:
            return []

        where_filter = build_chroma_filter(
            user_context,
            category=category,
            clearance_level=clearance_level,
        )
        collection_count = self._collection_count()
        if collection_count == 0:
            return []
        vector_limit = vector_k or max(k * 4, k)
        if collection_count > 0:
            vector_limit = min(vector_limit, collection_count)
        vector_results = self._vector_search(
            clean_query,
            vector_limit,
            where_filter,
        )
        keyword_docs = self._keyword_search(
            clean_query,
            keyword_k or max(k * 4, k),
            where_filter,
        )
        candidates = self._fuse_results(
            vector_results=vector_results,
            keyword_docs=keyword_docs,
            vector_weight=vector_weight,
            keyword_weight=keyword_weight,
        )
        if not candidates:
            return []

        candidates = candidates[: (rerank_top_n or max(k * 3, k))]
        reranker_name: str | None = None
        if rerank:
            candidates, reranker_name = self._rerank(clean_query, candidates, top_n=k)
        else:
            candidates = candidates[:k]

        return [
            RetrievalResult(
                document=candidate.document,
                score=candidate.score,
                vector_score=candidate.vector_score,
                keyword_score=candidate.keyword_score,
                rerank_score=candidate.rerank_score,
                vector_rank=candidate.vector_rank,
                keyword_rank=candidate.keyword_rank,
                reranker=reranker_name,
            )
            for candidate in candidates[:k]
        ]

    def retrieve_documents(self, query: str, **kwargs: Any) -> list[Document]:
        return [result.document for result in self.retrieve(query, **kwargs)]

    def _collection_count(self) -> int:
        try:
            return int(self.vectorstore._collection.count())
        except Exception:
            return -1

    def _vector_search(
        self,
        query: str,
        limit: int,
        where_filter: dict[str, Any] | None,
    ) -> list[tuple[Document, float | None]]:
        try:
            raw_results = self.vectorstore.similarity_search_with_score(
                query,
                k=limit,
                filter=where_filter,
            )
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            return []
        return [(document, 1.0 / (1.0 + max(float(score), 0.0))) for document, score in raw_results]

    def _keyword_search(
        self,
        query: str,
        limit: int,
        where_filter: dict[str, Any] | None,
    ) -> list[Document]:
        candidates = self._documents_matching_filter(where_filter)
        if not candidates:
            return []
        try:
            retriever = BM25Retriever.from_documents(candidates, preprocess_func=tokenize)
            retriever.k = limit
            return retriever.invoke(query)
        except Exception as exc:
            logger.warning("Keyword search failed: %s", exc)
            return []

    def _documents_matching_filter(self, where_filter: dict[str, Any] | None) -> list[Document]:
        try:
            if where_filter:
                raw = self.vectorstore.get(where=where_filter, include=["documents", "metadatas"])
            else:
                raw = self.vectorstore.get(include=["documents", "metadatas"])
        except Exception as exc:
            logger.warning("Unable to load keyword candidates from Chroma: %s", exc)
            return []

        documents = raw.get("documents") or []
        metadatas = raw.get("metadatas") or []
        return [
            Document(page_content=content, metadata=metadata or {})
            for content, metadata in zip(documents, metadatas, strict=False)
            if content
        ]

    def _fuse_results(
        self,
        *,
        vector_results: Sequence[tuple[Document, float | None]],
        keyword_docs: Sequence[Document],
        vector_weight: float,
        keyword_weight: float,
        rrf_k: int = 60,
    ) -> list[_Candidate]:
        candidates: dict[str, _Candidate] = {}

        for rank, (document, relevance_score) in enumerate(vector_results, start=1):
            key = _document_key(document)
            candidate = candidates.setdefault(key, _Candidate(document=document))
            candidate.vector_rank = rank
            candidate.vector_score = relevance_score
            candidate.score += vector_weight / (rrf_k + rank)

        for rank, document in enumerate(keyword_docs, start=1):
            key = _document_key(document)
            candidate = candidates.setdefault(key, _Candidate(document=document))
            candidate.keyword_rank = rank
            candidate.keyword_score = 1.0 / rank
            candidate.score += keyword_weight / (rrf_k + rank)

        return sorted(candidates.values(), key=lambda candidate: candidate.score, reverse=True)

    def _rerank(
        self,
        query: str,
        candidates: Sequence[_Candidate],
        *,
        top_n: int,
    ) -> tuple[list[_Candidate], str]:
        compressor = self.reranker or FlashrankRerank(top_n=top_n)
        reranked_docs = list(
            compressor.compress_documents(
                [candidate.document for candidate in candidates],
                query=query,
            )
        )
        by_key = {_document_key(candidate.document): candidate for candidate in candidates}
        reranked = [
            by_key[_document_key(document)]
            for document in reranked_docs
            if _document_key(document) in by_key
        ]
        return reranked[:top_n], compressor.__class__.__name__


@lru_cache(maxsize=8)
def _get_kb_rag(persist_directory: str, collection_name: str) -> KnowledgeBaseRAG:
    """Return a cached KnowledgeBaseRAG so the vectorstore and embedding model
    are initialised only once per (directory, collection) pair per process."""
    return KnowledgeBaseRAG(
        persist_directory=persist_directory,
        collection_name=collection_name,
    )


class HybridRAGPipeline:
    """Compatibility facade used by the rest of the app."""

    def __init__(
        self,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()

    def ingest(self) -> int:
        report = ingest_markdown_kb(
            kb_dir=self.settings.kb_dir,
            persist_directory=self.settings.chroma_dir,
            collection_name=DEFAULT_COLLECTION_NAME,
        )
        return report.chunks

    def retrieve(self, query: str, context: RetrievalContext, k: int = 5) -> list[Document]:
        rag = _get_kb_rag(str(self.settings.chroma_dir), DEFAULT_COLLECTION_NAME)
        return rag.retrieve_documents(query, user_context=context, k=k)

    def article_refs(self, docs: list[Document]) -> list[KBArticleRef]:
        refs: list[KBArticleRef] = []
        seen: set[str] = set()
        for index, doc in enumerate(docs):
            metadata = doc.metadata
            kb_id = str(metadata.get("kb_id") or metadata.get("source_id") or f"kb-{index}")
            if kb_id in seen:
                continue
            seen.add(kb_id)
            refs.append(
                KBArticleRef(
                    kb_id=kb_id,
                    title=str(metadata.get("title") or kb_id),
                    source=str(metadata.get("source") or kb_id),
                    relevance_score=max(0.0, min(1.0, 1.0 - (index * 0.12))),
                    clearance=_metadata_clearance(metadata),
                )
            )
        return refs


def build_chroma_filter(
    context: RetrievalContext | Mapping[str, Any] | Any | None = None,
    *,
    user_context: RetrievalContext | Mapping[str, Any] | Any | None = None,
    category: TicketCategory | str | None = None,
    clearance_level: int | str | UserClearance | None = None,
) -> dict[str, Any] | None:
    """Build a Chroma metadata filter before vector similarity search."""

    active_context = user_context if user_context is not None else context
    context_dict = _context_to_dict(active_context)

    selected_clearance = clearance_level
    if selected_clearance is None:
        selected_clearance = context_dict.get(
            "clearance_level",
            context_dict.get(
                "clearance",
                context_dict.get("user_clearance", DEFAULT_CLEARANCE_LEVEL),
            ),
        )
    clearance_level_int = _coerce_clearance_level(selected_clearance)
    clauses: list[dict[str, Any]] = [
        {
            "$or": [
                {"clearance_level": {"$lte": clearance_level_int}},
                {"clearance": {"$in": clearance_values(selected_clearance)}},
            ]
        }
    ]

    selected_category = category or context_dict.get("category")
    if selected_category:
        category_values = _expand_category_values(selected_category)
        clauses.append({"category": {"$in": category_values}})
    else:
        allowed_categories = context_dict.get("allowed_categories")
        category_values = _expand_many_categories(allowed_categories)
        if category_values:
            clauses.append({"category": {"$in": category_values}})

    app_name = context_dict.get("app_name")
    if app_name:
        clauses.append({"app_name": {"$in": [_normalize_term(app_name), "general"]}})

    environment = context_dict.get("environment")
    if environment and _normalize_term(environment) != "unknown":
        clauses.append({"environment": {"$in": [_normalize_term(environment), "unknown"]}})

    roles = _normalize_many(
        context_dict.get("roles")
        or context_dict.get("role")
        or context_dict.get("audiences")
        or context_dict.get("audience")
    )
    if roles:
        clauses.append({"audience": {"$in": sorted({"all", *roles})}})

    department = context_dict.get("department")
    if department:
        clauses.append({"department": {"$in": sorted({"all", _normalize_term(department)})}})

    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def clearance_values(user_clearance: UserClearance | str | int | None) -> list[str]:
    level = _coerce_clearance_level(
        user_clearance if user_clearance is not None else DEFAULT_CLEARANCE_LEVEL
    )
    values = [UserClearance.PUBLIC.value]
    if level >= 1:
        values.append(UserClearance.INTERNAL.value)
    if level >= 2:
        values.append(UserClearance.RESTRICTED.value)
    return values


def context_from_user(
    *,
    category: TicketCategory | None = None,
    app_name: str | None = None,
    environment: Environment | str = Environment.UNKNOWN,
    clearance: UserClearance = UserClearance.PUBLIC,
) -> RetrievalContext:
    environment_value = environment.value if hasattr(environment, "value") else str(environment)
    return RetrievalContext(
        category=category,
        app_name=app_name,
        environment=environment_value,
        user_clearance=clearance,
    )


def format_context(results: Sequence[RetrievalResult], *, max_chars_per_chunk: int = 1400) -> str:
    blocks: list[str] = []
    for index, result in enumerate(results, start=1):
        metadata = result.document.metadata
        content = result.document.page_content.strip()
        if len(content) > max_chars_per_chunk:
            content = f"{content[:max_chars_per_chunk].rstrip()}..."
        blocks.append(
            "\n".join(
                [
                    f"[{index}] {metadata.get('title', 'Untitled')}",
                    f"source: {metadata.get('source', 'unknown')}",
                    f"category: {metadata.get('category', 'general')}",
                    content,
                ]
            )
        )
    return "\n\n".join(blocks)


def _metadata_clearance(metadata: Mapping[str, Any]) -> UserClearance:
    value = metadata.get("clearance", metadata.get("clearance_level", UserClearance.PUBLIC.value))
    label = _clearance_label(_coerce_clearance_level(value))
    return UserClearance(label)


def _context_to_dict(context: RetrievalContext | Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if context is None:
        return {}
    if isinstance(context, RetrievalContext):
        return {
            "category": context.category,
            "app_name": context.app_name,
            "environment": context.environment,
            "user_clearance": context.user_clearance,
        }
    if isinstance(context, Mapping):
        return dict(context)
    if hasattr(context, "model_dump"):
        return context.model_dump(exclude_none=True)
    logger.debug("Unsupported context type %s; ignoring extra context fields", type(context))
    return {}


def _expand_many_categories(value: Any) -> list[str]:
    expanded = {
        category
        for item in _normalize_many(value)
        for category in _expand_category_values(item)
    }
    return sorted(expanded)


def _expand_category_values(value: TicketCategory | str | Any) -> list[str]:
    raw_value = value.value if hasattr(value, "value") else value
    normalized = _normalize_term(raw_value)
    return CATEGORY_ALIASES.get(normalized, [normalized])


def _document_key(document: Document) -> str:
    metadata = document.metadata
    chunk_id = metadata.get("chunk_id")
    if chunk_id:
        return str(chunk_id)
    return _stable_id(f"{metadata.get('source', '')}:{metadata.get('chunk_index', '')}")
