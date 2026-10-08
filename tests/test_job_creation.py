"""
test_job_creation.py – Tests for POST /api/v1/jobs job creation.

Covers requirement #24 from the traceability table.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from conftest import make_payload, wait_for_job
from app.models import Certificate, Job


class TestJobCreation:
    """Requirement: creating a generation job."""

    def test_returns_202_with_expected_fields(self, client):
        """POST returns 202 with job_id, status, total, accepted, rejected, status_url."""
        resp = client.post("/api/v1/jobs", json=make_payload(3))
        assert resp.status_code == 202
        data = resp.json()
        assert "job_id" in data
        assert data["status"] == "PENDING"
        assert data["total"] == 3
        assert data["accepted"] == 3
        assert data["rejected"] == 0
        assert "status_url" in data
        assert data["status_url"].endswith(data["job_id"])

    def test_job_row_persisted(self, client, db):
        """After POST, a Job row exists in the DB with correct total_count."""
        resp = client.post("/api/v1/jobs", json=make_payload(5))
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job = db.get(Job, job_id)
        assert job is not None
        assert job.total_count == 5
        assert job.event_name == "Test Event"

    def test_certificate_rows_persisted(self, client, db):
        """After POST, one Certificate row exists per recipient."""
        resp = client.post("/api/v1/jobs", json=make_payload(4))
        job_id = resp.json()["job_id"]

        certs = db.query(Certificate).filter(Certificate.job_id == job_id).all()
        assert len(certs) == 4
        # row_index should be 0-based sequential
        indices = sorted(c.row_index for c in certs)
        assert indices == [0, 1, 2, 3]

    def test_bulk_100_recipients_one_job(self, client, db):
        """100 recipients create exactly ONE job (not 100 separate jobs)."""
        resp = client.post("/api/v1/jobs", json=make_payload(100))
        assert resp.status_code == 202
        total_jobs = db.query(Job).count()
        assert total_jobs == 1
        job_id = resp.json()["job_id"]
        cert_count = (
            db.query(Certificate).filter(Certificate.job_id == job_id).count()
        )
        assert cert_count == 100

    def test_issue_date_defaults_to_today(self, client, db):
        """If issue_date is omitted, the job's issue_date is today (UTC)."""
        from datetime import datetime, timezone

        resp = client.post("/api/v1/jobs", json=make_payload(1))
        job_id = resp.json()["job_id"]
        job = db.get(Job, job_id)
        today = datetime.now(timezone.utc).date()
        assert job.issue_date == today

    def test_unknown_job_id_returns_404(self, client):
        """GET /api/v1/jobs/{unknown_id} returns 404."""
        resp = client.get("/api/v1/jobs/nonexistent-uuid-12345")
        assert resp.status_code == 404

    def test_status_url_points_to_working_endpoint(self, client):
        """The status_url in the creation response resolves to a 200."""
        resp = client.post("/api/v1/jobs", json=make_payload(2))
        status_url = resp.json()["status_url"]
        status_resp = client.get(status_url)
        assert status_resp.status_code == 200

    def test_accepted_rejected_counts_reported(self, client):
        """Rejected count is correct when some recipients are invalid."""
        payload = make_payload(0)  # base payload with no recipients
        payload["recipients"] = [
            {"name": "Valid Person", "email": "valid@example.com"},
            {"name": "", "email": "another@example.com"},  # blank name → invalid
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1
        assert data["total"] == 2

    def test_event_name_and_org_stored(self, client, db):
        """event_name and organization from the payload are persisted correctly."""
        payload = make_payload(1, event_name="PyData 2026", organization="Acme Co")
        resp = client.post("/api/v1/jobs", json=payload)
        job = db.get(Job, resp.json()["job_id"])
        assert job.event_name == "PyData 2026"
        assert job.organization == "Acme Co"

    def test_optional_fields_stored(self, client, db):
        """Signatory name/title and description are persisted when provided."""
        payload = make_payload(1)
        payload.update(
            description="for completing the workshop",
            signatory_name="Dr. Smith",
            signatory_title="Director",
        )
        resp = client.post("/api/v1/jobs", json=payload)
        job = db.get(Job, resp.json()["job_id"])
        assert job.description == "for completing the workshop"
        assert job.signatory_name == "Dr. Smith"
        assert job.signatory_title == "Director"
