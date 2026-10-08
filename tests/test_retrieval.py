"""
test_retrieval.py – Tests for certificate and ZIP retrieval endpoints.

Covers requirement #29.
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest

from conftest import make_payload, wait_for_job
from app.models import Certificate, CertificateStatus


class TestSingleCertificateDownload:
    """Tests for GET /api/v1/certificates/{id}/download."""

    def _get_completed_cert_id(self, client, db, n=1):
        """Helper: submit a job and return a completed certificate id."""
        resp = client.post("/api/v1/jobs", json=make_payload(n))
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
        return cert.id, cert.certificate_number

    def test_download_returns_200_and_pdf(self, client, db):
        """Completed certificate download returns 200 application/pdf."""
        cert_id, cert_num = self._get_completed_cert_id(client, db)
        resp = client.get(f"/api/v1/certificates/{cert_id}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"

    def test_download_body_starts_with_pdf(self, client, db):
        """PDF body starts with %PDF magic bytes."""
        cert_id, _ = self._get_completed_cert_id(client, db)
        resp = client.get(f"/api/v1/certificates/{cert_id}/download")
        assert resp.content[:4] == b"%PDF"

    def test_download_has_correct_filename_header(self, client, db):
        """Content-Disposition header contains the certificate number."""
        cert_id, cert_num = self._get_completed_cert_id(client, db)
        resp = client.get(f"/api/v1/certificates/{cert_id}/download")
        disposition = resp.headers.get("content-disposition", "")
        assert cert_num in disposition

    def test_unknown_cert_id_returns_404(self, client):
        resp = client.get("/api/v1/certificates/does-not-exist/download")
        assert resp.status_code == 404

    def test_pending_cert_returns_409(self, client, db):
        """A PENDING certificate (before processing) returns 409."""
        # Create a job but intercept before processing by checking a fresh cert.
        # Use the DB directly to find a PENDING cert from a fresh job.
        resp = client.post("/api/v1/jobs", json=make_payload(1))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        # All are COMPLETED now; artificially set one back to PENDING.
        cert = (
            db.query(Certificate).filter(Certificate.job_id == job_id).first()
        )
        original_status = cert.status
        cert.status = CertificateStatus.PENDING.value
        db.commit()

        resp2 = client.get(f"/api/v1/certificates/{cert.id}/download")
        assert resp2.status_code == 409

        # Restore
        cert.status = original_status
        db.commit()

    def test_failed_cert_returns_409(self, client, db):
        """A FAILED certificate returns 409."""
        resp = client.post("/api/v1/jobs", json=make_payload(1))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        cert = (
            db.query(Certificate).filter(Certificate.job_id == job_id).first()
        )
        cert.status = CertificateStatus.FAILED.value
        db.commit()

        resp2 = client.get(f"/api/v1/certificates/{cert.id}/download")
        assert resp2.status_code == 409

    def test_db_completed_but_file_deleted_returns_404(
        self, client, db, test_settings
    ):
        """If file is missing on disk, returns 404, not 500."""
        cert_id, _ = self._get_completed_cert_id(client, db)

        cert = db.get(Certificate, cert_id)
        abs_path = os.path.join(test_settings.storage_dir, cert.file_path)
        os.remove(abs_path)

        resp = client.get(f"/api/v1/certificates/{cert_id}/download")
        assert resp.status_code == 404


class TestCertificateListEndpoint:
    """Tests for GET /api/v1/jobs/{job_id}/certificates."""

    def test_list_returns_all_certificates(self, client, db):
        resp = client.post("/api/v1/jobs", json=make_payload(5))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        list_resp = client.get(f"/api/v1/jobs/{job_id}/certificates")
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 5

    def test_list_ordered_by_row_index(self, client, db):
        resp = client.post("/api/v1/jobs", json=make_payload(4))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        list_resp = client.get(f"/api/v1/jobs/{job_id}/certificates")
        items = list_resp.json()["items"]
        indices = [i["row_index"] for i in items]
        assert indices == sorted(indices)

    def test_pagination_limit_offset(self, client, db):
        resp = client.post("/api/v1/jobs", json=make_payload(10))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        page1 = client.get(
            f"/api/v1/jobs/{job_id}/certificates?limit=3&offset=0"
        ).json()
        page2 = client.get(
            f"/api/v1/jobs/{job_id}/certificates?limit=3&offset=3"
        ).json()

        assert len(page1["items"]) == 3
        assert len(page2["items"]) == 3
        ids1 = {i["certificate_id"] for i in page1["items"]}
        ids2 = {i["certificate_id"] for i in page2["items"]}
        assert ids1.isdisjoint(ids2)

    def test_status_filter_completed(self, client, db):
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "G1", "email": "g1@example.com"},
            {"name": "", "email": "bad@example.com"},  # invalid
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        list_resp = client.get(
            f"/api/v1/jobs/{job_id}/certificates?status=COMPLETED"
        )
        data = list_resp.json()
        assert all(i["status"] == "COMPLETED" for i in data["items"])

    def test_invalid_status_filter_returns_422(self, client):
        resp = client.post("/api/v1/jobs", json=make_payload(1))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        list_resp = client.get(
            f"/api/v1/jobs/{job_id}/certificates?status=INVALID_STATUS"
        )
        assert list_resp.status_code == 422

    def test_download_url_non_null_for_completed(self, client, db):
        resp = client.post("/api/v1/jobs", json=make_payload(2))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        list_resp = client.get(
            f"/api/v1/jobs/{job_id}/certificates?status=COMPLETED"
        )
        items = list_resp.json()["items"]
        for item in items:
            assert item["download_url"] is not None

    def test_list_unknown_job_returns_404(self, client):
        resp = client.get("/api/v1/jobs/unknown-job/certificates")
        assert resp.status_code == 404


class TestZipDownload:
    """Tests for GET /api/v1/jobs/{job_id}/download."""

    def test_zip_download_returns_valid_zip(self, client):
        resp = client.post("/api/v1/jobs", json=make_payload(3))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        zip_resp = client.get(f"/api/v1/jobs/{job_id}/download")
        assert zip_resp.status_code == 200
        assert zip_resp.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(zip_resp.content))
        assert zf.testzip() is None  # no corrupt entries

    def test_zip_contains_exactly_completed_certificates(self, client, db):
        """ZIP has exactly one file per COMPLETED certificate."""
        resp = client.post("/api/v1/jobs", json=make_payload(5))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        completed_count = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.COMPLETED.value,
            )
            .count()
        )

        zip_resp = client.get(f"/api/v1/jobs/{job_id}/download")
        zf = zipfile.ZipFile(io.BytesIO(zip_resp.content))
        assert len(zf.namelist()) == completed_count

    def test_zip_file_names_are_slugged(self, client):
        """ZIP entries use <index>_<slug>.pdf naming."""
        resp = client.post("/api/v1/jobs", json=make_payload(2))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        zip_resp = client.get(f"/api/v1/jobs/{job_id}/download")
        zf = zipfile.ZipFile(io.BytesIO(zip_resp.content))
        for name in zf.namelist():
            assert name.endswith(".pdf")
            # Should be something like "1_recipient_1.pdf"
            assert "_" in name or name[0].isdigit()

    def test_zip_unknown_job_returns_404(self, client):
        resp = client.get("/api/v1/jobs/unknown-job-id/download")
        assert resp.status_code == 404

    def test_zip_no_completed_certificates_returns_409(self, client, db):
        """ZIP for a job with no completed certificates returns 409."""
        resp = client.post("/api/v1/jobs", json=make_payload(1))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        # Set all completed certs to FAILED to simulate no-completed state.
        certs = db.query(Certificate).filter(Certificate.job_id == job_id).all()
        for c in certs:
            c.status = CertificateStatus.FAILED.value
            c.file_path = None
        db.commit()

        zip_resp = client.get(f"/api/v1/jobs/{job_id}/download")
        assert zip_resp.status_code == 409
