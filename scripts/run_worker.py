from __future__ import annotations

import logging

from app.background_jobs import run_worker_loop
from app.db import init_db
from app.settings import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    settings.ensure_local_dirs()
    settings.configure_langsmith_environment()
    init_db()
    run_worker_loop()


if __name__ == "__main__":
    main()
