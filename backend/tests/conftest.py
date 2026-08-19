"""
Test fixtures.

The suite runs against SQLite through aiosqlite, with a fresh file-backed database per
test module. File-backed rather than in-memory because the application opens more than
one connection and ``:memory:`` gives each of them a different, empty database.

Encryption and signing keys are set before ``app.core.config`` is imported so the
settings object is built with them.
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import uuid
from pathlib import Path
from typing import AsyncIterator

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="burapay-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP / 'test.db'}")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY",
                      base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode())
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PUBLIC_BASE_URL", "https://busrapay.test")
os.environ.setdefault("BOOTSTRAP_ADMIN_EMAIL", "admin@busrapay.com")
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "TestAdminPassword123")
# Keep runs fast: the production floor of one transaction every two seconds would make
# a ten-transaction run take twenty seconds of wall clock in CI.
os.environ.setdefault("BENCHMARK_MIN_INTERVAL_SECONDS", "0")

from httpx import ASGITransport, AsyncClient          # noqa: E402

from app.core.config import settings                  # noqa: E402
from app.db.base import Base                          # noqa: E402
from app.db.session import dispose_engine, get_engine, get_sessionmaker  # noqa: E402
from app.main import app                              # noqa: E402
from app.services import runner                       # noqa: E402
from app.services.bootstrap import bootstrap          # noqa: E402


@pytest.fixture
async def db_session() -> AsyncIterator:
    # A database file per test. A benchmark run started by one test can still be
    # finishing when the next one begins, and SQLite would let it hold a write lock
    # on a database the next test is trying to rebuild.
    settings.database_url = f"sqlite+aiosqlite:///{_TMP / f'test-{uuid.uuid4().hex}.db'}"
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with get_sessionmaker()() as session:
        await bootstrap(session)
        yield session

    # A benchmark run started by a test keeps executing in the background. Left alone
    # it would still be writing to this database while the next test drops its tables.
    for task in list(runner._active_tasks.values()):
        task.cancel()
    if runner._active_tasks:
        await asyncio.gather(*runner._active_tasks.values(), return_exceptions=True)
        runner._active_tasks.clear()
    await dispose_engine()


@pytest.fixture
async def client(db_session) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app, with the schema already seeded.

    The app's own lifespan is skipped: the fixture has already created the schema and
    seeded, and the health probe loop would fire real network calls during tests.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api") as http:
        yield http


@pytest.fixture
async def admin_token(client: AsyncClient) -> str:
    response = await client.post("/v1/auth/login", json={
        "email": "admin@busrapay.com", "password": "TestAdminPassword123"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}
