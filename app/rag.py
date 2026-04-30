"""Compatibility facade for KB ingestion and retrieval modules."""

from __future__ import annotations

from app.rag_ingest import (DEFAULT_COLLECTION_NAME, DEFAULT_KB_DIR,
                            DEFAULT_KB_INDEX_NAME, IngestionReport,
                            get_vectorstore, ingest_markdown_kb,
                            load_markdown_documents, split_documents,
                            split_markdown_documents)
from app.rag_retrieve import (HybridRAGPipeline, KnowledgeBaseRAG,
                              RetrievalContext, RetrievalResult,
                              build_pinecone_filter, clearance_values,
                              context_from_user, format_context)


def ingest_knowledge_base() -> int:
    return HybridRAGPipeline().ingest()


__all__ = [
    "DEFAULT_COLLECTION_NAME",
    "DEFAULT_KB_INDEX_NAME",
    "DEFAULT_KB_DIR",
    "IngestionReport",
    "RetrievalContext",
    "RetrievalResult",
    "KnowledgeBaseRAG",
    "HybridRAGPipeline",
    "get_vectorstore",
    "load_markdown_documents",
    "split_markdown_documents",
    "split_documents",
    "ingest_markdown_kb",
    "ingest_knowledge_base",
    "build_pinecone_filter",
    "clearance_values",
    "context_from_user",
    "format_context",
]
