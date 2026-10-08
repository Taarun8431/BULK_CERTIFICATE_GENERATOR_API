"""
conftest.py – Shared pytest fixtures.

Key design:
- Each test gets its own in-memory SQLite database (StaticPool) so tests
  are completely isolated and run in parallel without file-system contention.
- STORAGE_DIR is set to a per-test tmp_path so no files reach ./storage.
- The FastAPI app's dependencies are overridden so the app uses the test
  DB and test storage dir.
- Starlette's TestClient runs BackgroundTasks synchronously before .post()
  returns, so wait_for_job is just a convenience that also works for real.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import Base, get_session_factory
from app.main import create_app
from app.services.certificate_generator import (
    CertificateData,
    CertificateGenerator,
    PdfCertificateGenerator,
    get_generator,
)


# ---------------------------------------------------------------------------
# Test settings + DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_settings(tmp_path) -> Settings:
    """Return a Settings object pointing at a temp storage dir."""
    return Settings(
        database_url="sqlite:///:memory:",
        storage_dir=str(tmp_path / "storage"),
        max_recipients_per_request=1000,
        max_name_length=100,
        log_level="WARNING",  # keep test output clean
    )


@pytest.fixture()
def test_engine(test_settings):
    """In-memory SQLite engine per test with foreign-key enforcement."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # same connection for all threads → in-memory persists
    )
    # Enable foreign keys
    from sqlalchemy import event, text

    @event.listens_for(engine, "connect")
    def set_fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def test_session_factory(test_engine) -> sessionmaker[Session]:
    """Return a sessionmaker bound to the per-test in-memory engine."""
    return sessionmaker(bind=test_engine, autocommit=False, autoflush=False)


@pytest.fixture()
def db(test_session_factory) -> Session:
    """Yield a single Session for tests that need direct DB access."""
    session = test_session_factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# App + client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(test_settings, test_session_factory, tmp_path):
    """Build the app with dependency overrides for the test DB and storage."""
    import os

    os.makedirs(os.path.join(test_settings.storage_dir, "certificates"), exist_ok=True)

    fastapi_app = create_app()

    # Override dependencies so the app uses our test DB and settings.
    fastapi_app.dependency_overrides[get_session_factory] = (
        lambda: test_session_factory
    )
    fastapi_app.dependency_overrides[
        __import__("app.config", fromlist=["get_settings"]).get_settings
    ] = lambda: test_settings

    yield fastapi_app

    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def client(app) -> TestClient:
    """TestClient wrapping the overridden app.

    TestClient runs BackgroundTasks synchronously, so after client.post()
    returns, the job has already been processed.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: build a request payload
# ---------------------------------------------------------------------------


def make_payload(
    n: int = 2,
    event_name: str = "Test Event",
    organization: str = "Test Org",
    **overrides: Any,
) -> dict[str, Any]:
    """Build a valid POST /api/v1/jobs payload with *n* recipients."""
    recipients = [
        {"name": f"Recipient {i}", "email": f"recipient{i}@example.com"}
        for i in range(1, n + 1)
    ]
    payload: dict[str, Any] = {
        "event_name": event_name,
        "organization": organization,
        "recipients": recipients,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Helper: poll until job is done (handles real async too)
# ---------------------------------------------------------------------------


def wait_for_job(
    client: TestClient,
    job_id: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Poll GET /api/v1/jobs/{job_id} until status is terminal.

    With TestClient, BackgroundTasks run before .post() returns, so this
    typically succeeds on the first poll.  The timeout keeps the test robust
    if the fixture is reused against a real server.
    """
    deadline = time.monotonic() + timeout
    terminal = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        if data["status"] in terminal:
            return data
        time.sleep(0.1)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


# ---------------------------------------------------------------------------
# FailingGenerator fixture
# ---------------------------------------------------------------------------


class FailingGenerator:
    """A CertificateGenerator that raises RuntimeError for chosen names.

    Usage:
        gen = FailingGenerator(fail_for_names={"Asha Rao"})
        # Generates normally for everyone else.
    """

    def __init__(self, fail_for_names: set[str]) -> None:
        self._fail_for = fail_for_names
        self._real = PdfCertificateGenerator()

    def generate(self, data: CertificateData) -> bytes:
        if data.recipient_name in self._fail_for:
            raise RuntimeError(
                f"Simulated failure for recipient: {data.recipient_name}"
            )
        return self._real.generate(data)


@pytest.fixture()
def failing_generator_factory():
    """Return a factory function for creating FailingGenerator instances."""
    return lambda fail_for_names: FailingGenerator(fail_for_names)


def make_client_with_generator(
    test_settings: Settings,
    test_session_factory: sessionmaker[Session],
    generator: CertificateGenerator,
) -> TestClient:
    """Build an app+client with a custom generator injected."""
    import os

    os.makedirs(
        os.path.join(test_settings.storage_dir, "certificates"), exist_ok=True
    )

    fastapi_app = create_app()
    fastapi_app.dependency_overrides[get_session_factory] = (
        lambda: test_session_factory
    )
    fastapi_app.dependency_overrides[
        __import__("app.config", fromlist=["get_settings"]).get_settings
    ] = lambda: test_settings
    fastapi_app.dependency_overrides[get_generator] = lambda: generator

    return TestClient(fastapi_app, raise_server_exceptions=False)
