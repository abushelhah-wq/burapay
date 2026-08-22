"""
Administrative command line (specification section 10).

    python -m app.cli create-admin
    python -m app.cli create-admin --username ops --email ops@example.com
    python -m app.cli list-users
    python -m app.cli reset-password --username ops

The secure bootstrap path for the first administrator, and the way back in when the
only administrator account is locked out. Three rules hold throughout:

* A password is read from a hidden prompt, or from ``BURAPAY_ADMIN_PASSWORD`` in the
  environment for a scripted install. It is never accepted as a command-line argument,
  because arguments are visible in ``ps`` and in shell history.
* A password is never printed, never echoed and never logged.
* Nothing is hard-coded. Without a prompt and without the environment variable, the
  command fails rather than inventing a default.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select

from app.core.security import PasswordPolicyError, hash_password, validate_password
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_sessionmaker
from app.models import User, UserRole, UserStatus

#: The scripted-install escape hatch. Read once and cleared from the process
#: environment so it is not inherited by anything this process later spawns.
PASSWORD_ENV = "BURAPAY_ADMIN_PASSWORD"


def _read_password(confirm: bool = True) -> str:
    """A password from the environment or a hidden prompt. Never from argv."""
    from_env = os.environ.pop(PASSWORD_ENV, None)
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise SystemExit(
            f"No password available: set {PASSWORD_ENV} for an unattended install, "
            "or run this command on a terminal.")
    password = getpass.getpass("Password: ")
    if confirm and password != getpass.getpass("Confirm password: "):
        raise SystemExit("The passwords do not match.")
    return password


def _validated(password: str, *, username: str, email: str) -> str:
    try:
        validate_password(password, username=username, email=email)
    except PasswordPolicyError as exc:
        raise SystemExit(
            "The password must contain " + ", ".join(exc.problems) + ".") from exc
    return password


async def _ensure_schema() -> None:
    """Create the tables if migrations have not run.

    Makes ``create-admin`` usable against a brand-new database — the exact situation
    the command exists for — without depending on the API having started first.
    """
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def create_admin(username: Optional[str], email: Optional[str],
                       full_name: Optional[str], role: str) -> int:
    await _ensure_schema()
    username = (username or input("Username: ")).strip().lower()
    email = (email or input("Email: ")).strip().lower()
    if not username or not email:
        raise SystemExit("A username and an email address are both required.")

    password = _validated(_read_password(), username=username, email=email)

    async with get_sessionmaker()() as session:
        clash = (await session.execute(
            select(User).where(or_(func.lower(User.username) == username,
                                   func.lower(User.email) == email)))).scalars().first()
        if clash is not None:
            # Idempotent by refusal rather than by overwrite: a command that silently
            # reset an existing administrator's password would be a back door.
            print(f"An account already exists with that username or email "
                  f"({clash.username}). Use reset-password to change its password.",
                  file=sys.stderr)
            return 1

        session.add(User(username=username, email=email,
                         full_name=(full_name or "").strip() or None,
                         hashed_password=hash_password(password),
                         role=role, status=UserStatus.ACTIVE.value,
                         password_changed_at=datetime.now(timezone.utc)))
        await session.commit()
    print(f"Created {role} account {username} <{email}>. "
          "Change the password after the first sign-in.")
    return 0


async def reset_password(username: str) -> int:
    async with get_sessionmaker()() as session:
        needle = username.strip().lower()
        user = (await session.execute(
            select(User).where(or_(func.lower(User.username) == needle,
                                   func.lower(User.email) == needle)))).scalars().first()
        if user is None:
            print(f"No account matches {username!r}.", file=sys.stderr)
            return 1
        password = _validated(_read_password(), username=user.username, email=user.email)
        user.hashed_password = hash_password(password)
        user.password_changed_at = datetime.now(timezone.utc)
        # Also the way back in from a brute-force lockout.
        user.failed_login_count = 0
        user.locked_at = None
        if user.status == UserStatus.LOCKED.value:
            user.status = UserStatus.ACTIVE.value
        await session.commit()
    print(f"Password reset for {username}.")
    return 0


async def list_users() -> int:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(User).order_by(User.created_at))).scalars()
        print(f"{'USERNAME':<24} {'EMAIL':<32} {'ROLE':<8} {'STATUS':<10} LAST LOGIN")
        for user in rows:
            last = user.last_login_at.isoformat(timespec="seconds") if user.last_login_at else "never"
            print(f"{user.username:<24} {user.email:<32} {user.role:<8} "
                  f"{user.status:<10} {last}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli",
                                     description="BuraPay administrative commands.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin",
                            help="Create an administrator account interactively.")
    create.add_argument("--username")
    create.add_argument("--email")
    create.add_argument("--full-name", dest="full_name")
    create.add_argument("--role", default=UserRole.ADMIN.value,
                        choices=[role.value for role in UserRole],
                        help="Defaults to ADMIN.")

    reset = sub.add_parser("reset-password", help="Set a new password for an account.")
    reset.add_argument("--username", required=True,
                       help="Username or email address.")

    sub.add_parser("list-users", help="List accounts, their roles and their status.")
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    try:
        if args.command == "create-admin":
            return await create_admin(args.username, args.email, args.full_name, args.role)
        if args.command == "reset-password":
            return await reset_password(args.username)
        return await list_users()
    finally:
        await dispose_engine()


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_dispatch(args))


if __name__ == "__main__":
    raise SystemExit(main())
