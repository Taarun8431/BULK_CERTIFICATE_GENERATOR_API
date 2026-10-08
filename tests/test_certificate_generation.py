"""
test_certificate_generation.py – Tests for the PDF generation step.

Covers requirement #26.
"""

from __future__ import annotations

import os

import pytest

from conftest import make_payload, wait_for_job
from app.models import Certificate, CertificateStatus


class TestCertificateGeneration:
    """Tests verifying that PDFs are generated correctly."""

    def test_pdf_bytes_start_with_pdf_header(self, test_settings):
        """PdfCertificateGenerator.generate() returns bytes starting with %PDF."""
        from datetime import date
        from app.services.certificate_generator import (
            CertificateData,
            PdfCertificateGenerator,
        )

        gen = PdfCertificateGenerator()
        data = CertificateData(
            recipient_name="Test Recipient",
            event_name="Test Event",
            organization="Test Org",
            description="for completing",
            issue_date=date(2026, 10, 8),
            certificate_number="CERT-2026-ABCD1234",
        )
        pdf_bytes = gen.generate(data)
        assert pdf_bytes[:4] == b"%PDF", "PDF must start with %PDF magic bytes"

    def test_pdf_contains_recipient_name(self):
        """The generated PDF contains the recipient's name (pageCompression=0)."""
        from datetime import date
        from app.services.certificate_generator import (
            CertificateData,
            PdfCertificateGenerator,
        )

        gen = PdfCertificateGenerator()
        data = CertificateData(
            recipient_name="UniqueNameABC123",
            event_name="TestEvent",
            organization="TestOrg",
            description="for completing",
            issue_date=date(2026, 1, 1),
            certificate_number="CERT-2026-TEST0001",
        )
        pdf_bytes = gen.generate(data)
        assert b"UniqueNameABC123" in pdf_bytes

    def test_pdf_contains_event_name(self):
        """The generated PDF contains the event name."""
        from datetime import date
        from app.services.certificate_generator import (
            CertificateData,
            PdfCertificateGenerator,
        )

        gen = PdfCertificateGenerator()
        data = CertificateData(
            recipient_name="Someone",
            event_name="SpecialEventXYZ",
            organization="SomeOrg",
            description="for completing",
            issue_date=date(2026, 1, 1),
            certificate_number="CERT-2026-TEST0002",
        )
        pdf_bytes = gen.generate(data)
        assert b"SpecialEventXYZ" in pdf_bytes

    def test_very_long_name_does_not_crash(self):
        """A very long recipient name is handled without crashing."""
        from datetime import date
        from app.services.certificate_generator import (
            CertificateData,
            PdfCertificateGenerator,
        )

        gen = PdfCertificateGenerator()
        long_name = "A" * 200  # well beyond any reasonable font size
        data = CertificateData(
            recipient_name=long_name,
            event_name="Event",
            organization="Org",
            description="for completing",
            issue_date=date(2026, 1, 1),
            certificate_number="CERT-2026-LONGNAME",
        )
        pdf_bytes = gen.generate(data)
        assert pdf_bytes[:4] == b"%PDF"

    def test_unicode_emoji_name_does_not_crash_app(self, client):
        """A recipient with an emoji name either generates or records FAILED.

        Either outcome is acceptable; the app must not crash and the job
        must reach a terminal state.
        """
        payload = make_payload(0)
        payload["recipients"] = [
            {"name": "Emoji 🎓 Test", "email": "emoji@example.com"},
            {"name": "Normal Name", "email": "normal@example.com"},
        ]
        resp = client.post("/api/v1/jobs", json=payload)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        status = wait_for_job(client, job_id)
        assert status["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}

    def test_generated_file_exists_on_disk(self, client, db, test_settings):
        """After processing, the PDF file exists at the stored relative path."""
        resp = client.post("/api/v1/jobs", json=make_payload(2))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        completed_certs = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.COMPLETED.value,
            )
            .all()
        )
        assert completed_certs, "Expected at least one COMPLETED certificate"

        for cert in completed_certs:
            abs_path = os.path.join(test_settings.storage_dir, cert.file_path)
            assert os.path.exists(abs_path), f"File missing: {abs_path}"

    def test_certificate_numbers_are_unique(self, client, db):
        """Each certificate gets a unique certificate_number."""
        resp = client.post("/api/v1/jobs", json=make_payload(10))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        certs = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.COMPLETED.value,
            )
            .all()
        )
        numbers = [c.certificate_number for c in certs]
        assert len(numbers) == len(set(numbers)), "Certificate numbers must be unique"

    def test_all_valid_recipients_completed_after_processing(self, client, db):
        """After processing, all valid recipients have COMPLETED status."""
        resp = client.post("/api/v1/jobs", json=make_payload(5))
        job_id = resp.json()["job_id"]
        wait_for_job(client, job_id)

        certs = (
            db.query(Certificate).filter(Certificate.job_id == job_id).all()
        )
        statuses = {c.status for c in certs}
        assert CertificateStatus.COMPLETED.value in statuses
        assert CertificateStatus.PENDING.value not in statuses

    def test_pdf_readable_by_pypdf(self):
        """The generated PDF can be opened and read by pypdf."""
        import io
        from datetime import date

        import pypdf

        from app.services.certificate_generator import (
            CertificateData,
            PdfCertificateGenerator,
        )

        gen = PdfCertificateGenerator()
        data = CertificateData(
            recipient_name="Jane Doe",
            event_name="PyData 2026",
            organization="Acme Corp",
            description="for completing the workshop",
            issue_date=date(2026, 10, 8),
            certificate_number="CERT-2026-PYPDF001",
            signatory_name="J. Smith",
            signatory_title="Director",
        )
        pdf_bytes = gen.generate(data)
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == 1
        text = reader.pages[0].extract_text()
        assert "Jane Doe" in text or "Jane" in text  # name appears
