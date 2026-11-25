"""Set the break-glass password for an admin: python scripts/set_admin_password.py admin@example.com

The password is read with getpass, never from the command line, and must be at least 12 characters.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import argparse
import getpass
import sys

from setuhaul.auth import local_admin, users
from setuhaul.config import load_settings
from setuhaul.db import open_connection
from setuhaul.identity import AuthError


def main(argv: list[str] | None = None) -> int:
    # The admin's email is the only argument; the password is prompted for.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    args = parser.parse_args(argv)
    password = getpass.getpass("New password: ")
    if len(password) < local_admin.MIN_LENGTH:
        print(f"Refused: the password must be at least {local_admin.MIN_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat password: ") != password:
        print("Refused: the passwords do not match.", file=sys.stderr)
        return 1
    conn = open_connection(path=load_settings().db_path)
    try:
        user = users.find_by_email(conn, args.email)
        # Only an active admin may hold a break-glass password.
        if user is None or user.role != "ADMIN" or user.status != "ACTIVE":
            print("Refused: no active admin with that email.", file=sys.stderr)
            return 1
        try:
            local_admin.set_password(conn, user.user_id, password)
        except AuthError as exc:
            print(f"Refused: {exc.code}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"Password set for {user.email}. Enable the route with SETUHAUL_LOCAL_ADMIN=1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
