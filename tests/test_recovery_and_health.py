"""
test_recovery_and_health.py – Tests for health endpoint and startup recovery.

Covers: /health endpoint, startup recovery, settings defaults.
"""

from __future__ import annotations

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.services.processor import recover_interrupted_jobs


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_health_body_shape(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "status" in data


class TestStartupRecovery:
    """Tests for recover_interrupted_jobs called at startup."""

    def _make_factory(self):
        """Create an isolated in-memory DB for recovery tests."""
        from sqlalchemy import event as sa_event

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @sa_event.listens_for(engine, "connect")
        def set_fk(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
        return factory

    def test_pending_job_marked_failed_on_recovery(self):
        """A job in PENDING state is marked FAILED by recovery."""
        from datetime import date

        factory = self._make_factory()
        db = factory()

        job = Job(
            event_name="Test",
            organization="Org",
            issue_date=date.today(),
            status=JobStatus.PENDING.value,
            total_count=2,
        )
        db.add(job)
        db.flush()
        job_id = job.id

        cert1 = Certificate(
            job_id=job_id,
            row_index=0,
            recipient_name="Alice",
            recipient_email="alice@example.com",
            status=CertificateStatus.PENDING.value,
        )
        cert2 = Certificate(
            job_id=job_id,
            row_index=1,
            recipient_name="Bob",
            recipient_email="bob@example.com",
            status=CertificateStatus.PENDING.value,
        )
        db.add_all([cert1, cert2])
        db.commit()
        db.close()

        recover_interrupted_jobs(factory)

        db2 = factory()
        job_after = db2.get(Job, job_id)
        assert job_after.status == JobStatus.FAILED.value
        assert job_after.error_message == "interrupted by server restart"

        certs = db2.query(Certificate).filter(Certificate.job_id == job_id).all()
        assert all(c.status == CertificateStatus.FAILED.value for c in certs)
        db2.close()

    def test_processing_job_marked_failed_on_recovery(self):
        """A job in PROCESSING state is marked FAILED by recovery."""
        from datetime import date

        factory = self._make_factory()
        db = factory()

        job = Job(
            event_name="Test",
            organization="Org",
            issue_date=date.today(),
            status=JobStatus.PROCESSING.value,
            total_count=1,
        )
        db.add(job)
        db.flush()
        job_id = job.id

        cert = Certificate(
            job_id=job_id,
            row_index=0,
            recipient_name="Carol",
            recipient_email="carol@example.com",
            status=CertificateStatus.PENDING.value,
        )
        db.add(cert)
        db.commit()
        db.close()

        recover_interrupted_jobs(factory)

        db2 = factory()
        job_after = db2.get(Job, job_id)
        assert job_after.status in (JobStatus.FAILED.value, JobStatus.COMPLETED_WITH_ERRORS.value)
        db2.close()

    def test_partially_completed_job_recovery(self):
        """A PROCESSING job with some COMPLETED certs → COMPLETED_WITH_ERRORS."""
        from datetime import date

        factory = self._make_factory()
        db = factory()

        job = Job(
            event_name="Test",
            organization="Org",
            issue_date=date.today(),
            status=JobStatus.PROCESSING.value,
            total_count=2,
        )
        db.add(job)
        db.flush()
        job_id = job.id

        completed_cert = Certificate(
            job_id=job_id,
            row_index=0,
            recipient_name="Done",
            recipient_email="done@example.com",
            status=CertificateStatus.COMPLETED.value,
            certificate_number="CERT-2026-DONE0001",
        )
        pending_cert = Certificate(
            job_id=job_id,
            row_index=1,
            recipient_name="Pending",
            recipient_email="pending@example.com",
            status=CertificateStatus.PENDING.value,
        )
        db.add_all([completed_cert, pending_cert])
        db.commit()
        db.close()

        recover_interrupted_jobs(factory)

        db2 = factory()
        job_after = db2.get(Job, job_id)
        # Some completed + some failed → COMPLETED_WITH_ERRORS
        assert job_after.status == JobStatus.COMPLETED_WITH_ERRORS.value
        db2.close()

    def test_completed_jobs_not_affected_by_recovery(self):
        """Already-COMPLETED jobs are not touched by recovery."""
        from datetime import date

        factory = self._make_factory()
        db = factory()

        job = Job(
            event_name="Test",
            organization="Org",
            issue_date=date.today(),
            status=JobStatus.COMPLETED.value,
            total_count=1,
        )
        db.add(job)
        db.flush()
        job_id = job.id
        db.commit()
        db.close()

        recover_interrupted_jobs(factory)

        db2 = factory()
        job_after = db2.get(Job, job_id)
        assert job_after.status == JobStatus.COMPLETED.value
        db2.close()


class TestSettingsDefaults:
    """Tests that Settings has correct defaults."""

    def test_settings_default_database_url(self):
        from app.config import Settings

        s = Settings()
        assert "sqlite" in s.database_url

    def test_settings_default_max_recipients(self):
        from app.config import Settings

        s = Settings()
        assert s.max_recipients_per_request == 1000

    def test_settings_default_max_name_length(self):
        from app.config import Settings

        s = Settings()
        assert s.max_name_length == 100


class TestAppStartsWithNoDb:
    """Verify the app creates its own DB/storage on startup."""

    def test_app_creates_storage_dir(self, client, test_settings):
        """The app startup creates the storage/certificates directory.

        The 'client' fixture triggers app startup which creates the dirs.
        """
        import os

        assert os.path.isdir(
            os.path.join(test_settings.storage_dir, "certificates")
        )
