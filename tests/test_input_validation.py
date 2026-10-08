"""
test_input_validation.py – Tests for request and per-recipient validation.

Covers requirement #25.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from conftest import make_payload, wait_for_job
from app.models import Certificate, CertificateStatus, Job


class TestRequestLevelValidation:
    """Tests for request-level (Pydantic) validation."""

    def test_empty_recipients_list_returns_422(self, client):
        payload = make_payload(0)
        payload["recipients"] = []
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422

    def test_missing_event_name_returns_422(self, client):
        payload = make_payload(1)
        del payload["event_name"]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422

    def test_blank_event_name_returns_422(self, client):
        payload = make_payload(1)
        payload["event_name"] = "   "
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422

    def test_missing_organization_returns_422(self, client):
        payload = make_payload(1)
        del payload["organization"]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422

    def test_blank_organization_returns_422(self, client):
        payload = make_payload(1)
        payload["organization"] = ""
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422

    def test_recipients_not_a_list_returns_422(self, client):
        payload = make_payload(1)
        payload["recipients"] = "not a list"
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422

    def test_malformed_json_returns_422(self, client):
        resp = client.post(
            "/api/v1/jobs",
            content=b"{bad json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_too_many_recipients_returns_422(self, client, test_settings):
        """More than MAX_RECIPIENTS_PER_REQUEST → 422."""
        n = test_settings.max_recipients_per_request + 1
        payload = make_payload(n)
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422

    def test_error_body_has_consistent_shape(self, client):
        """All 422 errors return {"detail": ..., "errors": [...]}."""
        payload = make_payload(1)
        del payload["event_name"]
        resp = client.post("/api/v1/jobs", json=payload)
        body = resp.json()
        assert "detail" in body
        assert "errors" in body

    def test_all_invalid_recipients_returns_422_no_job(self, client, db):
        """If all recipients are invalid, no job is created and 422 is returned."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "", "email": "valid@example.com"},
            {"name": "Valid Name", "email": "not-an-email"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 422
        assert db.query(Job).count() == 0


class TestPerRecipientValidation:
    """Tests for per-recipient validation (invalid ones become INVALID rows)."""

    def test_invalid_email_recorded_as_invalid(self, client, db):
        """A recipient with an invalid email becomes an INVALID certificate row."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "Good Person", "email": "good@example.com"},
            {"name": "Bad Email", "email": "not-an-email"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        invalid = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.INVALID.value,
            )
            .all()
        )
        assert len(invalid) == 1
        assert "email" in invalid[0].error_message.lower() or invalid[0].error_message

    def test_blank_name_recorded_as_invalid(self, client, db):
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "  ", "email": "blank@example.com"},
            {"name": "Fine", "email": "fine@example.com"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202

        job_id = resp.json()["job_id"]
        invalid = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.INVALID.value,
            )
            .all()
        )
        assert len(invalid) == 1

    def test_overlong_name_recorded_as_invalid(self, client, db, test_settings):
        long_name = "A" * (test_settings.max_name_length + 1)
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": long_name, "email": "long@example.com"},
            {"name": "Normal", "email": "normal@example.com"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202

        job_id = resp.json()["job_id"]
        invalid = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.INVALID.value,
            )
            .all()
        )
        assert len(invalid) == 1

    def test_non_object_recipient_recorded_as_invalid(self, client, db):
        """A non-dict entry (integer, null, string) is recorded as INVALID."""
        payload = make_payload(0)
        payload["recipients"] = [
            5,
            None,
            "text",
            {"name": "Valid", "email": "valid@example.com"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        invalid = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.INVALID.value,
            )
            .all()
        )
        assert len(invalid) == 3

    def test_duplicate_email_within_request(self, client, db):
        """Second occurrence of same email is INVALID with 'duplicate email' message."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "First", "email": "dup@example.com"},
            {"name": "Second", "email": "dup@example.com"},  # duplicate
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        invalid = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.INVALID.value,
            )
            .all()
        )
        assert len(invalid) == 1
        assert "duplicate" in invalid[0].error_message.lower()

    def test_name_whitespace_trimmed(self, client, db):
        """Leading/trailing whitespace in name is trimmed before storage."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "  Asha Rao  ", "email": "asha@example.com"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        job_id = resp.json()["job_id"]

        cert = (
            db.query(Certificate).filter(Certificate.job_id == job_id).first()
        )
        assert cert.recipient_name == "Asha Rao"

    def test_email_lowercased(self, client, db):
        """Email is normalized to lowercase."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "Asha", "email": "Asha@EXAMPLE.COM"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        job_id = resp.json()["job_id"]

        cert = (
            db.query(Certificate).filter(Certificate.job_id == job_id).first()
        )
        assert cert.recipient_email == "asha@example.com"

    def test_extra_fields_ignored(self, client):
        """Unknown fields in recipient objects are silently ignored."""
        payload = make_payload(0)
        payload["recipients"] = [
            {
                "name": "Valid",
                "email": "valid@example.com",
                "extra_field": "should be ignored",
                "another": 123,
            }
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 1

    def test_valid_and_invalid_mixed_job_created(self, client):
        """With a mix, a job is created (202) and counts are correct."""
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "Good", "email": "good@example.com"},
            {"name": "", "email": "bad@example.com"},  # invalid
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1
        assert data["total"] == 2
