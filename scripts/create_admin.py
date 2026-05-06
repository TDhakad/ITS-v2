"""One-time script to create an admin user.

Usage:
    uv run python scripts/create_admin.py
    uv run python scripts/create_admin.py --email admin@example.com --name "Admin" --password secret
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on the path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_password
from app.db import SessionLocal, get_user_by_email, init_db
from app.schemas import UserClearance, UserRole


def create_admin(email: str, display_name: str, password: str) -> None:
    init_db()
    with SessionLocal() as db:
        existing = get_user_by_email(db, email)
        if existing:
            if existing.role == UserRole.ADMIN.value:
                print(f"Admin already exists: {existing.email}")
            else:
                existing.role = UserRole.ADMIN.value
                existing.clearance = UserClearance.RESTRICTED.value
                db.commit()
                print(f"Promoted existing user to admin: {existing.email}")
            return

        from app.db import UserRecord

        record = UserRecord(
            email=email.casefold(),
            display_name=display_name,
            hashed_password=hash_password(password),
            role=UserRole.ADMIN.value,
            clearance=UserClearance.RESTRICTED.value,
        )
        db.add(record)
        db.commit()
        print(f"Admin created: {email}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an admin user for ITS Helpdesk."
    )
    parser.add_argument("--email", default="admin@its.local")
    parser.add_argument("--name", default="Admin")
    parser.add_argument(
        "--password",
        default=None,
        help="If omitted, you will be prompted (input hidden).",
    )
    args = parser.parse_args()

    password = args.password
    if not password:
        import getpass

        password = getpass.getpass(f"Password for {args.email}: ")
        if not password:
            print("Password cannot be empty.")
            sys.exit(1)

    create_admin(args.email, args.name, password)


if __name__ == "__main__":
    main()
