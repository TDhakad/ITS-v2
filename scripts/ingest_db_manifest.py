import sys
from pathlib import Path
import argparse

from langchain_core.documents import Document

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.schema import CreateTable

from app.db import Base, engine
from app.rag_ingest import get_vectorstore
from app.settings import get_settings

SCHEMA_QUERY_EXAMPLES = {
    "tickets": (
        "Examples: component -> tickets.app_name; title -> tickets.summary; "
        "status -> tickets.status."
    ),
    "ticket_tags": (
        "Examples: ticket tags/components require ticket_tags joined to tickets "
        "and tags."
    ),
    "tags": "Examples: component label, tag name, tag slug -> tags.name or tags.slug.",
    "users": (
        "Examples: requester, creator, user name, email -> users joined by "
        "tickets.user_id when compatible."
    ),
    "projects": "Examples: project name, team, workspace -> projects.",
    "project_members": (
        "Examples: project membership, team member, user project access -> "
        "project_members."
    ),
}


def extract_tables_ddl(table_names: list[str]) -> dict[str, str]:
    """
    Provide Create table DDL for tables, including column comments for LLM context.
    """
    schema_outputs = dict()

    for table_name in table_names:
        # Check to validate if table exists (Your TODO)
        if table_name not in Base.metadata.tables:
            print(f"Warning: Table '{table_name}' not found in metadata.")
            continue

        table = Base.metadata.tables[table_name]

        # 1. Extract the base DDL (CREATE TABLE ...)
        base_ddl = str(CreateTable(table).compile(engine)).strip()

        # 2. Extract column comments
        comments = []
        for column in table.columns:
            if column.comment:
                # Format clearly for the LLM
                comments.append(f"-- {column.name}: {column.comment}")

        # 3. Combine DDL with the comments
        if comments:
            comments_section = "\n".join(comments)
            # Append comments below the DDL so the LLM easily associates them
            full_context = f"{base_ddl}\n\nColumn Details:\n{comments_section}"
        else:
            full_context = base_ddl

        schema_outputs[table_name] = full_context

    return schema_outputs


def get_orm_join_hints(table_names: list[str]) -> dict[str, list[str]]:
    """
    Extracts relationship paths between requested tables.
    Returns a dictionary mapping each table name to a list of its join hints.
    """
    # Initialize dictionary for all requested tables
    hints_dict = {name: [] for name in table_names}
    seen_pairs = set()

    # Map string table names to their ORM Mappers dynamically
    table_to_mapper = {
        mapper.local_table.name: mapper for mapper in Base.registry.mappers
    }

    for table_name in table_names:
        if table_name not in table_to_mapper:
            print(f"Warning: No ORM mapper found for table '{table_name}'")
            continue

        mapper = table_to_mapper[table_name]

        for rel in mapper.relationships:
            target_table = rel.target.name

            # ONLY extract the join if the target table is also in our requested list
            if target_table in table_names and target_table != table_name:

                # Create a unique tuple to track if we've seen this pair
                tables_sorted = tuple(sorted([table_name, target_table]))

                if tables_sorted not in seen_pairs:
                    seen_pairs.add(tables_sorted)

                    local_col = list(rel.local_columns)[0].name
                    remote_col = list(rel.remote_side)[0].name

                    # Standardize the equality string
                    pair = sorted(
                        [f"{table_name}.{local_col}", f"{target_table}.{remote_col}"]
                    )

                    hint = f"To join '{tables_sorted[0]}' and '{tables_sorted[1]}', use {pair[0]} = {pair[1]}"

                    # Append the hint to BOTH tables so each key has all its relevant joins
                    hints_dict[table_name].append(hint)
                    hints_dict[target_table].append(hint)

    # Optional: Remove keys with empty lists to keep the dict clean
    return {k: v for k, v in hints_dict.items() if v}


def create_tables_manifest(table_names: list[str] | None = None):
    db_manifests = PROJECT_ROOT / "db_manifests"

    # Create dir if not exists
    db_manifests.mkdir(exist_ok=True)
    # If no names provided, grab them all
    if not table_names:
        table_names = list(Base.metadata.tables.keys())

    tables_dll = extract_tables_ddl(table_names)
    hints = get_orm_join_hints(table_names)

    from datetime import datetime

    current_timestamp = str(datetime.now())

    for table_name, details in tables_dll.items():
        with open(f"db_manifests/{table_name}.txt", "w") as file:
            timestamp_metadeta = (
                f"{'-' * 50} \n"
                + f"manifest update time: {current_timestamp}\n"
                + f"ingested time: {current_timestamp}\n"
                + f"{'-' * 50} \n"
            )
            details = (
                timestamp_metadeta
                + details
                + "\n\nHINTS: \n"
                + "\n".join(hints.get(table_name, []))
            )
            file.write(details)


def embed_models_docstring(table_names: list[str] | None = None) -> dict[str, str]:
    """Embed ORM model docstrings into the DB schema vector index.

    Each embedded document uses the model docstring as content and stores the
    source table name in metadata under the key "table".
    """
    if not table_names:
        table_names = list(Base.metadata.tables.keys())

    settings = get_settings()

    table_doc_map: dict[str, str] = {}
    table_to_mapper = {
        mapper.local_table.name: mapper for mapper in Base.registry.mappers
    }

    for table_name in table_names:
        mapper = table_to_mapper.get(table_name)
        if mapper is None:
            print(f"Warning: No ORM mapper found for table '{table_name}'")
            continue

        model_doc = (mapper.class_.__doc__ or "").strip()
        if not model_doc:
            print(f"Warning: No model docstring found for table '{table_name}'")
            continue
        table_doc_map[table_name] = model_doc

    if not table_doc_map:
        print("Warning: No model docstrings available to embed.")
        return {}

    vector_store = get_vectorstore(index_name=settings.pinecone_db_index_name)

    documents: list[Document] = []
    ids: list[str] = []
    for table_name, docstring in table_doc_map.items():
        content = schema_embedding_content(table_name, docstring)
        documents.append(
            Document(
                page_content=content,
                metadata={
                    "table": table_name,
                    "source": f"db_schema:{table_name}",
                    "source_type": "db_schema_manifest",
                },
            )
        )
        ids.append(f"db-model:{table_name}")

    # Upsert by stable IDs so reruns refresh the same records.
    vector_store.add_documents(documents, ids=ids)
    return table_doc_map


def schema_embedding_content(table_name: str, docstring: str) -> str:
    manifest_path = PROJECT_ROOT / "db_manifests" / f"{table_name}.txt"
    manifest = manifest_path.read_text() if manifest_path.exists() else ""
    example = SCHEMA_QUERY_EXAMPLES.get(table_name, "")
    return "\n\n".join(
        part
        for part in [
            f"Table: {table_name}",
            docstring.strip(),
            manifest.strip(),
            example,
        ]
        if part
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Embed ORM model docstrings into the Pinecone DB schema index."
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=None,
        help=(
            "Optional list of table names to embed. "
            "If omitted, all mapped tables are embedded."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    table_names = args.tables if args.tables else None
    embedded = embed_models_docstring(table_names)
    print(f"Embedded model docstrings: {len(embedded)}")
    if embedded:
        print("Tables:", ", ".join(sorted(embedded.keys())))


if __name__ == "__main__":
    main()
