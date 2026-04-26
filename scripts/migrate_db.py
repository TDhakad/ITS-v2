"""Apply database migrations without starting the web app."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import engine, ensure_database_directory  # noqa: E402
from app.db_migrations import run_migrations  # noqa: E402


def main() -> None:
    ensure_database_directory()
    run_migrations(engine)
    print("Database migrations applied.")


if __name__ == "__main__":
    main()
