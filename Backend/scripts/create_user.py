"""
Create (or reset the password of) an admin user.

    cd Backend
    python -m scripts.create_user

Prompts for a username and password. Re-running with an existing username
updates that user's password.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
from database import close_pool, db_cursor  # noqa: E402


def main() -> int:
    username = input("Username: ").strip().lower()
    if not username:
        print("Username is required.")
        return 1

    role = input("Role [admin/editor/viewer] (admin): ").strip().lower() or "admin"
    if role not in ("admin", "editor", "viewer"):
        print("Role must be admin, editor, or viewer.")
        return 1

    password = getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8:
        print("Password too short.")
        return 1
    if password != getpass.getpass("Confirm password: "):
        print("Passwords do not match.")
        return 1

    pw_hash = auth.hash_password(password)
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (username)
                DO UPDATE SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role
                RETURNING id, (xmax = 0) AS created
                """,
                (username, pw_hash, role),
            )
            uid, created = cur.fetchone()
        print(f"User '{username}' ({role}) {'created' if created else 'updated'} (id {uid}).")
        return 0
    except Exception as exc:
        print(f"Failed: {exc}")
        return 1
    finally:
        close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
