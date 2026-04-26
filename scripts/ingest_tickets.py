"""Index existing SQLite tickets into the Pinecone ticket index."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal, init_db, list_tickets  # noqa: E402
from app.ticket_vector import DEFAULT_TICKET_INDEX_NAME, index_tickets  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Append/upsert into the existing ticket index instead of clearing it first.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db()
    with SessionLocal() as db:
        tickets = list_tickets(db, None, limit=args.limit)
    count = index_tickets(tickets, reset=not args.no_reset)
    print(
        "\n".join(
            [
                "Tickets indexed.",
                f"index_name: {DEFAULT_TICKET_INDEX_NAME}",
                f"tickets: {count}",
            ]
        )
    )


if __name__ == "__main__":
    main()
