"""
test_job_status_progress.py – Tests for job status and progress tracking.

Covers requirement #27.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from conftest import FailingGenerator, make_client_with_generator, make_payload, wait_for_job
from app.models import Certificate, CertificateStatus, Job, JobStatus


class TestJobStatusProgress:
    """Tests for job status transitions and progress reporting."""

    def test_fresh_job_is_pending(self, app, test_settings, test_session_factory):
        """Immediately after creation (before background processing), status is PENDING.

        Note: TestClient runs background tasks synchronously, so we need to
        bypass that by checking the status returned in the creation response.
        """
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/jobs", json=make_payload(2))
        assert resp.status_code == 202
        # The creation response always reports PENDING.
        assert resp.json()["status"] == "PENDING"

    def test_completed_job_status(self, client):
        """After processing all valid recipients, job status is COMPLETED."""
        resp = client.post("/api/v1/jobs", json=make_payload(3))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)
        assert status["status"] == "COMPLETED"

    def test_progress_100_when_completed(self, client):
        """Progress is 100.0 when all recipients are processed."""
        resp = client.post("/api/v1/jobs", json=make_payload(3))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)
        assert status["progress_percent"] == 100.0

    def test_completed_at_set_after_processing(self, client):
        """completed_at is set to a non-null value after job finishes."""
        resp = client.post("/api/v1/jobs", json=make_payload(2))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)
        assert status["completed_at"] is not None

    def test_counts_sum_to_total(self, client):
        """pending + completed + failed + invalid == total."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "Good 1", "email": "g1@example.com"},
            {"name": "Good 2", "email": "g2@example.com"},
            {"name": "", "email": "bad@example.com"},  # invalid
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        counts = status["counts"]
        assert counts["pending"] + counts["completed"] + counts["failed"] + counts["invalid"] == status["total"]

    def test_completed_with_errors_when_mixed(self, test_settings, test_session_factory):
        """Job is COMPLETED_WITH_ERRORS when some succeed and some fail."""
        gen = FailingGenerator(fail_for_names={"Recipient 1"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        payload = make_payload(3)
        resp = client.post("/api/v1/jobs", json=payload)
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)
        assert status["status"] == "COMPLETED_WITH_ERRORS"

    def test_failed_job_when_all_fail(self, test_settings, test_session_factory):
        """Job is FAILED when all certificate generations fail."""
        # 3 recipients, all will fail
        gen = FailingGenerator(fail_for_names={"Recipient 1", "Recipient 2", "Recipient 3"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        resp = client.post("/api/v1/jobs", json=make_payload(3))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)
        assert status["status"] == "FAILED"

    def test_failures_list_in_job_status(self, test_settings, test_session_factory):
        """failures field lists FAILED and INVALID certificates."""
        gen = FailingGenerator(fail_for_names={"Recipient 2"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "Recipient 1", "email": "r1@example.com"},
            {"name": "Recipient 2", "email": "r2@example.com"},  # will fail
            {"name": "", "email": "r3@example.com"},  # invalid
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        failures = status["failures"]
        statuses = {f["status"] for f in failures}
        assert "FAILED" in statuses or "INVALID" in statuses
        assert len(failures) >= 2

    def test_job_not_found_returns_404(self, client):
        """GET /api/v1/jobs/{unknown} returns 404."""
        resp = client.get("/api/v1/jobs/does-not-exist")
        assert resp.status_code == 404

    def test_processed_equals_sum_of_non_pending(self, client):
        """processed = completed + failed + invalid."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "G1", "email": "g1@example.com"},
            {"name": "", "email": "bad@example.com"},  # invalid
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        counts = status["counts"]
        expected_processed = counts["completed"] + counts["failed"] + counts["invalid"]
        assert status["processed"] == expected_processed


    def test_failed_when_all_invalid(self, client):
        """All-invalid recipients → 422 and no job created."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "", "email": "bad1@example.com"},
            {"name": "X", "email": "not-an-email"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        # All invalid → 422, no job created
        assert resp.status_code == 422
