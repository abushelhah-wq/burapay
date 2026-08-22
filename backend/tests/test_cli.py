"""
The administrative CLI (specification section 10).

What is being checked here is mostly what the command *refuses* to do: accept a
password on the command line, invent one when none is available, overwrite an existing
account, or print a password anywhere. Those are the properties that make it a secure
bootstrap rather than a convenient one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import cli
from app.core.security import verify_password
from app.db.session import get_sessionmaker
from app.models import User, UserRole, UserStatus


@pytest.fixture
def scripted_password(monkeypatch):
    """Supply a password the way an unattended install does."""
    def _set(value: str) -> None:
        monkeypatch.setenv(cli.PASSWORD_ENV, value)
    return _set


class TestCreateAdmin:
    async def test_creates_a_hashed_administrator(self, db_session, scripted_password):
        scripted_password("Bootstrap-Secret9!")
        code = await cli.create_admin("ops", "ops@busrapay.com", "Ops Person",
                                      UserRole.ADMIN.value)
        assert code == 0

        async with get_sessionmaker()() as session:
            user = (await session.execute(
                select(User).where(User.username == "ops"))).scalar_one()
        assert user.role == UserRole.ADMIN.value
        assert user.status == UserStatus.ACTIVE.value
        # Hashed, not stored, not reversible.
        assert user.hashed_password != "Bootstrap-Secret9!"
        assert verify_password("Bootstrap-Secret9!", user.hashed_password)

    async def test_the_password_is_never_printed(self, db_session, scripted_password,
                                                 capsys):
        scripted_password("Bootstrap-Secret9!")
        await cli.create_admin("quiet", "quiet@busrapay.com", None, UserRole.ADMIN.value)
        captured = capsys.readouterr()
        assert "Bootstrap-Secret9!" not in captured.out
        assert "Bootstrap-Secret9!" not in captured.err

    async def test_an_existing_account_is_refused_not_overwritten(
            self, db_session, scripted_password, capsys):
        """Silently resetting an administrator's password would be a back door."""
        scripted_password("Bootstrap-Secret9!")
        await cli.create_admin("dupe", "dupe@busrapay.com", None, UserRole.ADMIN.value)

        scripted_password("Different-Secret9!")
        code = await cli.create_admin("dupe", "dupe@busrapay.com", None,
                                      UserRole.ADMIN.value)
        assert code == 1

        async with get_sessionmaker()() as session:
            user = (await session.execute(
                select(User).where(User.username == "dupe"))).scalar_one()
        assert verify_password("Bootstrap-Secret9!", user.hashed_password)

    async def test_a_weak_password_is_refused(self, db_session, scripted_password):
        scripted_password("password")
        with pytest.raises(SystemExit):
            await cli.create_admin("weak", "weak@busrapay.com", None,
                                   UserRole.ADMIN.value)

    async def test_no_password_available_fails_rather_than_defaulting(
            self, db_session, monkeypatch):
        monkeypatch.delenv(cli.PASSWORD_ENV, raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        with pytest.raises(SystemExit):
            await cli.create_admin("nopass", "nopass@busrapay.com", None,
                                   UserRole.ADMIN.value)

    def test_the_parser_offers_no_way_to_pass_a_password(self):
        """Arguments show up in ``ps`` and in shell history."""
        parser = cli.build_parser()
        actions = {action.dest for action in parser._subparsers._group_actions[0]
                   .choices["create-admin"]._actions}
        assert "password" not in actions


class TestResetPassword:
    async def test_reset_clears_a_lockout(self, db_session, scripted_password):
        scripted_password("Bootstrap-Secret9!")
        await cli.create_admin("locked", "locked@busrapay.com", None,
                               UserRole.USER.value)

        async with get_sessionmaker()() as session:
            user = (await session.execute(
                select(User).where(User.username == "locked"))).scalar_one()
            user.status = UserStatus.LOCKED.value
            user.failed_login_count = 9
            await session.commit()

        scripted_password("Recovered-Secret9!")
        assert await cli.reset_password("locked") == 0

        async with get_sessionmaker()() as session:
            user = (await session.execute(
                select(User).where(User.username == "locked"))).scalar_one()
        assert user.status == UserStatus.ACTIVE.value
        assert user.failed_login_count == 0
        assert verify_password("Recovered-Secret9!", user.hashed_password)

    async def test_an_unknown_account_is_reported(self, db_session, scripted_password,
                                                  capsys):
        scripted_password("Recovered-Secret9!")
        assert await cli.reset_password("ghost") == 1
