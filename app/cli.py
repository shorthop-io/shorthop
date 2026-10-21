"""Command line for operators: `python -m app.cli <command>`, run inside the container.

    admin EMAIL [--password-stdin]   make EMAIL an admin and set their sign-in password

The way into a fresh install that has no Google SSO, and the way back in when every admin is
locked out. Whoever can run commands in the container already controls the install, so this
grants nothing new. The password is typed at a prompt (twice), or read from stdin with
`--password-stdin` for automation — never taken from an argument or an environment variable,
where it would land in shell history, `ps` and the container's inspect output.
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys

from .audit import record
from .auth import hash_password
from .db import SessionLocal, init_db
from .models import User

MIN_LENGTH = 12        # the same rule as PUT /api/v1/users/<id>/password
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password (at least 12 characters): ")
    if getpass.getpass("Again: ") != first:
        sys.exit("The two passwords differ; nothing was changed.")
    return first


def make_admin(email: str, password: str) -> str:
    """Create or promote EMAIL to an enabled admin with this password. Returns what happened."""
    email = email.strip().lower()
    if not _EMAIL.match(email):
        raise ValueError(f"{email!r} is not an email address")
    if not (MIN_LENGTH <= len(password) <= 200):
        raise ValueError(f"the password must be {MIN_LENGTH} to 200 characters")
    db = SessionLocal
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, name="", role="admin")
        db.add(user)
        what = "created"
    else:
        what = "updated"
    user.role, user.disabled = "admin", False
    user.password_hash = hash_password(password)
    db.commit()
    record("cli", "user.admin.cli", target=email, detail=what)
    return what


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("admin", help="make EMAIL an admin and set their sign-in password")
    a.add_argument("email")
    a.add_argument("--password-stdin", action="store_true",
                   help="read the password from the first line of stdin instead of prompting")
    args = p.parse_args(argv)

    init_db()
    try:
        what = make_admin(args.email, _read_password(args.password_stdin))
    except ValueError as e:
        sys.exit(str(e))
    finally:
        SessionLocal.remove()
    print(f"{args.email.strip().lower()}: admin {what}; sign in with the password at the web UI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
