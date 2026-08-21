"""
Test harness.

Two deliberate choices about fidelity:

* **Tests run against real Postgres**, not SQLite. The idempotency guarantee is
  a UNIQUE constraint and the audit log depends on a second connection seeing
  committed rows -- neither is meaningfully exercised by an in-memory SQLite
  database, and testing a different engine from the one that is deployed would
  make the tests worth less than they look.
* **The mock gateway is a real HTTP server on a real port.** The shared client
  builds its own ``httpx.AsyncClient``, so injecting a transport would bypass
  exactly the code path under test -- connection handling, timeouts, status
  codes. A localhost server exercises all of it.

What these tests do NOT prove: that Geidea accepts these requests. The mock
implements the shapes this repository's documentation study recorded, so the
tests verify *our* sequencing, call counting, redaction and state machine.
Only a live sandbox run verifies the wire format, and the doc pages needed to
be confident about it were unreachable when this was built -- see
app/gateways/geidea/endpoints.py.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Iterator

# Environment must be set before anything imports app.config, which caches
# Settings on first use.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://burapay:burapay@127.0.0.1:5432/burapay_test"
)
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-used-in-production")
os.environ.setdefault("ENCRYPTION_KEY", "aVeryTestOnlyEncryptionPassphrase-000000000")
os.environ.setdefault("PUBLIC_BASE_URL", "https://test.example")
os.environ.setdefault("ALLOW_DIRECT_CARD_ENTRY", "1")
os.environ.setdefault("ALLOW_PRODUCTION_GATEWAYS", "0")
os.environ.setdefault("HTTP_TIMEOUT_SECONDS", "5")
os.environ.setdefault("BENCHMARK_MIN_INTERVAL_SECONDS", "0")
os.environ.setdefault("BENCHMARK_MAX_TRANSACTIONS_PER_RUN", "5")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import get_sessionmaker, session_scope  # noqa: E402
from app.models import Base  # noqa: E402

from tests.mock_geidea import build_mock_app, state as mock_state  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # pragma: no cover
        # Running inside a thread; uvicorn's handlers would fail there.
        pass


@pytest.fixture(scope="session")
def mock_gateway() -> Iterator[str]:
    """Start the mock gateway and yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(
        build_mock_app(), host="127.0.0.1", port=port, log_level="error",
    )
    server = _Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover
        raise RuntimeError("mock gateway failed to start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(autouse=True)
def reset_mock() -> Iterator[None]:
    mock_state.reset()
    yield


@pytest_asyncio.fixture(scope="session", autouse=True)
async def schema() -> None:
    """Create the schema once for the session."""
    from app.db import get_engine

    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(schema) -> None:
    """
    Truncate between tests.

    TRUNCATE ... RESTART IDENTITY so ids are predictable, and CASCADE so the
    foreign keys between transactions and their logs do not dictate an ordering
    the test has to know about.
    """
    async with session_scope() as session:
        await session.execute(
            text(
                "TRUNCATE api_call_logs, webhooks_received, tokens, "
                "benchmark_runs, transactions, gateway_credentials, "
                "integration_types, gateways, users RESTART IDENTITY CASCADE"
            )
        )


@pytest_asyncio.fixture
async def db():
    async with session_scope() as session:
        yield session


@pytest_asyncio.fixture
async def geidea(mock_gateway: str, db):
    """
    A configured Geidea gateway pointed at the mock server.

    Credentials go in through CredentialStore, so the encryption path is
    exercised by every flow test rather than only by its own unit test.
    """
    from app.gateways.geidea.adapter import CRED_API_PASSWORD, CRED_PUBLIC_KEY
    from app.security.credentials import CredentialStore
    from app.services.bootstrap import ensure_reference_data, gateway_by_code

    await ensure_reference_data(db)
    await db.commit()

    gateway = await gateway_by_code(db, "geidea")
    gateway.config_json = {"api_base": mock_gateway, "region": "ksa"}

    store = CredentialStore(db)
    await store.put(
        gateway_id=gateway.id, key_name=CRED_PUBLIC_KEY, value="pk_test_abcdef123456"
    )
    await store.put(
        gateway_id=gateway.id, key_name=CRED_API_PASSWORD, value="apipassword_test_987654"
    )
    await db.commit()
    return gateway


@pytest.fixture
def settings():
    return get_settings()
