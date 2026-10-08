"""
test_failure_handling.py – Tests for per-certificate failure isolation.

Covers requirement #28.
"""

from __future__ import annotations

import os

import pytest

from conftest import (
    FailingGenerator,
    make_client_with_generator,
    make_payload,
    wait_for_job,
)
from app.models import Certificate, CertificateStatus, JobStatus


class TestFailureHandling:
    """Tests that individual certificate failures are isolated."""

    def test_one_failure_others_still_complete(self, test_settings, test_session_factory):
        """One failing recipient does not prevent others from completing."""
        gen = FailingGenerator(fail_for_names={"Recipient 2"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        resp = client.post("/api/v1/jobs", json=make_payload(4))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        counts = status["counts"]
        assert counts["completed"] == 3
        assert counts["failed"] == 1

    def test_failed_certificate_has_error_message(self, test_settings, test_session_factory, db):
        """The FAILED certificate row has a non-empty error_message."""
        gen = FailingGenerator(fail_for_names={"Recipient 1"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        resp = client.post("/api/v1/jobs", json=make_payload(2))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        from sqlalchemy.orm import Session
        db_local = test_session_factory()
        try:
            failed = (
                db_local.query(Certificate)
                .filter(
                    Certificate.job_id == job_id,
                    Certificate.status == CertificateStatus.FAILED.value,
                )
                .all()
            )
            assert len(failed) == 1
            assert failed[0].error_message
            assert "generation failed" in failed[0].error_message.lower()
        finally:
            db_local.close()

    def test_job_status_is_completed_with_errors(self, test_settings, test_session_factory):
        """Job becomes COMPLETED_WITH_ERRORS when some succeed and one fails."""
        gen = FailingGenerator(fail_for_names={"Recipient 3"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        resp = client.post("/api/v1/jobs", json=make_payload(4))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        assert status["status"] == "COMPLETED_WITH_ERRORS"

    def test_failures_list_contains_failed_recipient(self, test_settings, test_session_factory):
        """The failures field in job status lists the failing recipient."""
        gen = FailingGenerator(fail_for_names={"Recipient 1"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        resp = client.post("/api/v1/jobs", json=make_payload(3))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        failed_names = [
            f["recipient_name"]
            for f in status["failures"]
            if f["status"] == "FAILED"
        ]
        assert "Recipient 1" in failed_names

    def test_all_fail_job_is_failed(self, test_settings, test_session_factory):
        """If every generation fails, the job status is FAILED."""
        gen = FailingGenerator(
            fail_for_names={"Recipient 1", "Recipient 2", "Recipient 3"}
        )
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        resp = client.post("/api/v1/jobs", json=make_payload(3))
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        assert status["status"] == "FAILED"

    def test_no_pending_certs_after_processor_done(self, test_settings, test_session_factory):
        """After processing, no certificate remains PENDING."""
        gen = FailingGenerator(fail_for_names={"Recipient 2"})
        client = make_client_with_generator(test_settings, test_session_factory, gen)

        resp = client.post("/api/v1/jobs", json=make_payload(3))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        db = test_session_factory()
        try:
            pending = (
                db.query(Certificate)
                .filter(
                    Certificate.job_id == job_id,
                    Certificate.status == CertificateStatus.PENDING.value,
                )
                .count()
            )
            assert pending == 0
        finally:
            db.close()

    def test_file_missing_at_download_returns_clean_error(
        self, client, db, test_settings
    ):
        """If a completed file is deleted, download returns 404 (not 500)."""
        resp = client.post("/api/v1/jobs", json=make_payload(1))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        cert = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.COMPLETED.value,
            )
            .first()
        )
        assert cert is not None

        # Delete the file to simulate missing file scenario.
        abs_path = os.path.join(test_settings.storage_dir, cert.file_path)
        os.remove(abs_path)

        dl_resp = client.get(f"/api/v1/certificates/{cert.id}/download")
        assert dl_resp.status_code == 404

    def test_os_replace_failure_handled(
        self, test_settings, test_session_factory, monkeypatch
    ):
        """A failure at the file-write step is caught and recorded as FAILED."""
        import app.services.processor as proc_module

        original_write = proc_module.write_pdf_atomically
        calls = [0]

        def failing_write(pdf_bytes, dest_path):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError("Simulated disk write failure")
            return original_write(pdf_bytes, dest_path)

        monkeypatch.setattr(proc_module, "write_pdf_atomically", failing_write)

        from app.services.certificate_generator import PdfCertificateGenerator
        client = make_client_with_generator(
            test_settings, test_session_factory, PdfCertificateGenerator()
        )

        # With 2 recipients, first write fails, second succeeds.
        resp = client.post("/api/v1/jobs", json=make_payload(2))
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)

        counts = status["counts"]
        # First cert should fail (OSError), second should succeed
        assert counts["failed"] == 1
        assert counts["completed"] == 1
        assert status["status"] == "COMPLETED_WITH_ERRORS"

    def test_unexpected_processor_exception_does_not_propagate(
        self, test_settings, test_session_factory, monkeypatch
    ):
        """An unexpected error in the processor is caught; job is marked FAILED.

        TestClient runs BackgroundTasks synchronously, so by the time
        client.post() returns, the background task (and its exception handling)
        has already completed.
        """
        import app.services.processor as proc_module

        def exploding_inner(*args, **kwargs):
            raise RuntimeError("Unexpected catastrophic failure")

        monkeypatch.setattr(proc_module, "_process_job_inner", exploding_inner)

        from app.services.certificate_generator import PdfCertificateGenerator
        client = make_client_with_generator(
            test_settings, test_session_factory, PdfCertificateGenerator()
        )

        resp = client.post("/api/v1/jobs", json=make_payload(2))
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # With TestClient, background tasks run before .post() returns.
        # The job should already be FAILED.
        status_resp = client.get(f"/api/v1/jobs/{job_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["status"] == "FAILED"
