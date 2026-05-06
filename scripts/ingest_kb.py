"""Ingest Markdown knowledge-base files into the Pinecone KB index."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag import (
    DEFAULT_KB_DIR,
    KnowledgeBaseRAG,  # noqa: E402
    format_context,
    ingest_markdown_kb,
)
from app.settings import get_settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--index-name", default=settings.pinecone_kb_index_name)
    parser.add_argument("--chunk-size", type=int, default=900)
    parser.add_argument("--chunk-overlap", type=int, default=120)
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Append/upsert into the existing index instead of clearing it first.",
    )
    parser.add_argument("--smoke-query", help="Run a retrieval check after ingestion.")
    parser.add_argument(
        "--role", default="employee", help="Role for the optional smoke query."
    )
    parser.add_argument(
        "--clearance-level",
        default="public",
        help="Clearance for the optional smoke query: public, internal, restricted, or a number.",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help=(
            "Use FlashRank in the optional smoke query. Without this flag, "
            "smoke query avoids model downloads."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = ingest_markdown_kb(
        kb_dir=args.kb_dir,
        index_name=args.index_name,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        reset=not args.no_reset,
    )

    print(
        "\n".join(
            [
                "Knowledge base ingested.",
                f"kb_dir: {report.kb_dir}",
                f"index_name: {report.index_name}",
                f"source_documents: {report.source_documents}",
                f"chunks: {report.chunks}",
            ]
        )
    )

    if args.smoke_query:
        rag = KnowledgeBaseRAG(
            index_name=args.index_name,
        )
        results = rag.retrieve(
            args.smoke_query,
            user_context={"role": args.role, "clearance": args.clearance_level},
            k=3,
            rerank=args.rerank,
        )
        print("\nSmoke query context:")
        print(format_context(results) if results else "No results.")


if __name__ == "__main__":
    main()
