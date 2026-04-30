"""Seed the database with default mock projects and optionally assign users.

Usage:
    uv run python scripts/seed_projects.py
    uv run python scripts/seed_projects.py --assign-admin admin@its.local
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import (SessionLocal, add_project_member, create_project,
                    get_user_by_email, init_db, list_projects)
from app.schemas import ProjectAccessLevel, ProjectCreate

MOCK_PROJECTS: list[dict[str, str]] = [
    {
        "name": "Data Analysis Tool",
        "slug": "data-analysis-tool",
        "description": (
            "Internal analytics platform for processing and visualising business "
            "intelligence datasets. Covers ETL pipelines, dashboard widgets, and "
            "reporting workflows."
        ),
    },
    {
        "name": "Government Loan Application",
        "slug": "govt-loan-application",
        "description": (
            "Citizen-facing web portal for submitting and tracking government-backed "
            "loan applications. Integrates with national identity services and "
            "financial assessment APIs."
        ),
    },
    {
        "name": "Smart Electricity",
        "slug": "smart-electricity",
        "description": (
            "IoT-enabled smart grid management system. Handles real-time metering, "
            "demand forecasting, outage detection, and customer billing for the "
            "electricity distribution network."
        ),
    },
]


def seed(assign_to_email: str | None = None) -> None:
    init_db()
    with SessionLocal() as db:
        existing_slugs = {p.slug for p in list_projects(db, active_only=False)}

        owner_user = None
        if assign_to_email:
            owner_user = get_user_by_email(db, assign_to_email)
            if owner_user is None:
                print(
                    f"[warn] User not found: {assign_to_email} — projects will have no owner."
                )

        created = 0
        for spec in MOCK_PROJECTS:
            if spec["slug"] in existing_slugs:
                print(f"[skip] '{spec['name']}' already exists.")
                continue

            record = create_project(
                db,
                ProjectCreate(
                    name=spec["name"],
                    slug=spec["slug"],
                    description=spec["description"],
                    owner_id=owner_user.id if owner_user else 1,
                ),
            )
            if owner_user:
                add_project_member(
                    db, record.id, owner_user.id, ProjectAccessLevel.OWNER.value
                )
            print(
                f"[ok]   Created project '{record.name}' (id={record.id}, slug={record.slug})"
            )
            created += 1

        print(
            f"\nDone. {created} project(s) created, {len(MOCK_PROJECTS) - created} skipped."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed mock projects into ITS Helpdesk."
    )
    parser.add_argument(
        "--assign-admin",
        metavar="EMAIL",
        default=None,
        help="Email of an existing user to set as OWNER of every seeded project.",
    )
    args = parser.parse_args()
    seed(assign_to_email=args.assign_admin)


if __name__ == "__main__":
    main()
