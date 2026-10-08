"""
job_service.py – Business logic for creating and querying jobs.

Responsibilities:
- create_job: validates individual recipients (keeping invalid ones as
  INVALID certificate rows), persists everything in one transaction,
  returns counts.
- get_job_summary: loads a job + derives progress from a GROUP BY query.
- list_certificates: paginated + filtered listing.
- build_zip: streams all COMPLETED PDFs for a job into a ZIP archive.

No HTTP or FastAPI concepts here – just database and domain logic.
"""

from __future__ import annotations

import io
import logging
import os
import re
import unicodedata
import zipfile
from datetime import date, datetime, timezone
from typing import Any, Optional

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.schemas import (
    CertificateItem,
    CertificateListResponse,
    FailureItem,
    JobStatusResponse,
    RecipientIn,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _validate_recipient(
    raw: Any,
    seen_emails: set[str],
    max_name_length: int,
) -> tuple[Optional[RecipientIn], Optional[str]]:
    """Validate a single raw recipient entry.

    Returns (RecipientIn, None) on success, or (None, error_message) on
    failure.  Updates *seen_emails* on success.
    """
    # Must be a dict/object
    if not isinstance(raw, dict):
        return None, "recipient must be a JSON object"

    # Run Pydantic validation
    try:
        rec = RecipientIn.model_validate(raw)
    except PydanticValidationError as exc:
        # Build a concise message from the first error.
        errors = exc.errors()
        msg = errors[0]["msg"] if errors else "invalid recipient data"
        # Strip "Value error, " prefix Pydantic v2 adds for field_validator.
        msg = re.sub(r"^Value error,\s*", "", msg)
        return None, msg

    # Check name length (RecipientIn already trimmed it)
    if len(rec.name) > max_name_length:
        return None, f"name exceeds maximum length of {max_name_length}"

    # Duplicate email check (within this request)
    email_lower = rec.email.lower()
    if email_lower in seen_emails:
        return None, "duplicate email in request"

    seen_emails.add(email_lower)
    return rec, None


def _slug(name: str) -> str:
    """Convert a name to a safe ASCII slug for ZIP file entries.

    Example: "Asha Rao" → "asha_rao"
    """
    # Normalize unicode (e.g. accented chars) to ASCII equivalents.
    try:
        normalized = unicodedata.normalize("NFKD", name)
        ascii_name = normalized.encode("ascii", errors="ignore").decode("ascii")
    except Exception:
        ascii_name = name

    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")
    return slug or "recipient"


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def create_job(
    db: Session,
    payload: Any,          # JobCreateRequest (already Pydantic-validated)
    settings: Settings,
) -> dict[str, Any]:
    """Validate recipients, persist job + certificates, return counts.

    The entire persistence step is one transaction so a crash between
    creating the Job and the Certificates never leaves orphan rows.

    Returns a dict with keys: job_id, status, total, accepted, rejected.
    Raises ValidationError if zero recipients are valid.
    """
    issue_date: date = payload.issue_date or _today_utc()

    seen_emails: set[str] = set()
    accepted: list[RecipientIn] = []
    invalid_rows: list[tuple[int, Any, str]] = []  # (index, raw, reason)

    for idx, raw in enumerate(payload.recipients):
        rec, error = _validate_recipient(raw, seen_emails, settings.max_name_length)
        if error:
            invalid_rows.append((idx, raw, error))
        else:
            accepted.append(rec)  # type: ignore[arg-type]

    if not accepted:
        raise ValidationError(
            "All recipients are invalid; no job created. "
            f"Errors: {[r[2] for r in invalid_rows[:5]]}"
        )

    total = len(payload.recipients)
    rejected = len(invalid_rows)

    # Build ORM objects
    job = Job(
        event_name=payload.event_name,
        organization=payload.organization,
        description=payload.description,
        issue_date=issue_date,
        signatory_name=payload.signatory_name,
        signatory_title=payload.signatory_title,
        status=JobStatus.PENDING.value,
        total_count=total,
    )
    db.add(job)
    db.flush()  # Assigns job.id without committing.

    # Valid recipients → PENDING certificates
    valid_idx_set = {
        idx for idx in range(total)
        if idx not in {r[0] for r in invalid_rows}
    }

    for idx, rec in zip(
        [i for i in range(total) if i not in {r[0] for r in invalid_rows}],
        accepted,
    ):
        cert = Certificate(
            job_id=job.id,
            row_index=idx,
            recipient_name=rec.name,
            recipient_email=rec.email,
            status=CertificateStatus.PENDING.value,
        )
        db.add(cert)

    # Invalid recipients → INVALID certificates
    for idx, raw, reason in invalid_rows:
        # Extract whatever name/email we can from the raw input for reporting.
        name_raw: Optional[str] = None
        email_raw: Optional[str] = None
        if isinstance(raw, dict):
            n = raw.get("name")
            e = raw.get("email")
            name_raw = str(n) if n is not None else None
            email_raw = str(e) if e is not None else None

        cert = Certificate(
            job_id=job.id,
            row_index=idx,
            recipient_name=name_raw,
            recipient_email=email_raw,
            raw_input=raw,
            status=CertificateStatus.INVALID.value,
            error_message=reason,
        )
        db.add(cert)

    db.commit()
    db.refresh(job)

    logger.info(
        "Job created id=%s total=%d accepted=%d rejected=%d",
        job.id, total, len(accepted), rejected,
    )

    return {
        "job_id": job.id,
        "status": job.status,
        "total": total,
        "accepted": len(accepted),
        "rejected": rejected,
    }


def get_job_summary(db: Session, job_id: str) -> JobStatusResponse:
    """Load a job and compute progress counts from certificate rows.

    Counts are derived from a GROUP BY query (never from stored counters)
    so they are always accurate even after partial processing or crashes.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")

    # Derive counts from certificate rows (single GROUP BY query)
    rows = db.execute(
        select(Certificate.status, func.count(Certificate.id).label("cnt"))
        .where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()

    counts: dict[str, int] = {
        "pending": 0,
        "completed": 0,
        "failed": 0,
        "invalid": 0,
    }
    for row in rows:
        key = row.status.lower()
        if key in counts:
            counts[key] = row.cnt

    total = job.total_count
    processed = counts["completed"] + counts["failed"] + counts["invalid"]
    progress_percent = round(processed / total * 100, 2) if total > 0 else 0.0

    # Failures = FAILED + INVALID certificates
    failures_rows = db.execute(
        select(Certificate)
        .where(
            Certificate.job_id == job_id,
            Certificate.status.in_(
                [CertificateStatus.FAILED.value, CertificateStatus.INVALID.value]
            ),
        )
        .order_by(Certificate.row_index)
    ).scalars().all()

    failures = [
        FailureItem(
            certificate_id=c.id,
            row_index=c.row_index,
            recipient_name=c.recipient_name,
            recipient_email=c.recipient_email,
            status=c.status,
            error_message=c.error_message,
        )
        for c in failures_rows
    ]

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        event_name=job.event_name,
        organization=job.organization,
        total=total,
        counts=counts,
        processed=processed,
        progress_percent=progress_percent,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_message=job.error_message,
        failures=failures,
        download_url=f"/api/v1/jobs/{job.id}/download",
    )


def list_certificates(
    db: Session,
    job_id: str,
    status_filter: Optional[str],
    limit: int,
    offset: int,
) -> CertificateListResponse:
    """Return a paginated, optionally filtered list of certificates."""
    # Verify job exists
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")

    base_q = select(Certificate).where(Certificate.job_id == job_id)

    if status_filter is not None:
        try:
            status_val = CertificateStatus(status_filter.upper())
        except ValueError:
            raise ValidationError(
                f"Invalid status filter '{status_filter}'. "
                f"Valid values: {[s.value for s in CertificateStatus]}"
            )
        base_q = base_q.where(Certificate.status == status_val.value)

    total_q = select(func.count()).select_from(base_q.subquery())
    total: int = db.execute(total_q).scalar_one()

    items_q = (
        base_q.order_by(Certificate.row_index)
        .limit(limit)
        .offset(offset)
    )
    certs = db.execute(items_q).scalars().all()

    items = [
        CertificateItem(
            certificate_id=c.id,
            row_index=c.row_index,
            recipient_name=c.recipient_name,
            recipient_email=c.recipient_email,
            status=c.status,
            certificate_number=c.certificate_number,
            error_message=c.error_message,
            download_url=(
                f"/api/v1/certificates/{c.id}/download"
                if c.status == CertificateStatus.COMPLETED.value
                else None
            ),
        )
        for c in certs
    ]

    return CertificateListResponse(
        job_id=job_id,
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


def build_zip(db: Session, job_id: str, storage_dir: str) -> bytes:
    """Build a ZIP archive of all COMPLETED certificates for a job.

    Each file inside the ZIP is named:
        <row_index+1>_<safe_slug_of_name>.pdf
    slugs are made unique by appending _2, _3 … if needed.

    Raises NotFoundError for unknown job_id.
    Raises ConflictError if no certificates are COMPLETED yet.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")

    completed = db.execute(
        select(Certificate)
        .where(
            Certificate.job_id == job_id,
            Certificate.status == CertificateStatus.COMPLETED.value,
        )
        .order_by(Certificate.row_index)
    ).scalars().all()

    if not completed:
        raise ConflictError("No completed certificates available for this job yet")

    buf = io.BytesIO()
    used_names: set[str] = set()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for cert in completed:
            # Build unique filename
            name = cert.recipient_name or "recipient"
            slug = _slug(name)
            base = f"{cert.row_index + 1}_{slug}"
            entry_name = f"{base}.pdf"
            counter = 2
            while entry_name in used_names:
                entry_name = f"{base}_{counter}.pdf"
                counter += 1
            used_names.add(entry_name)

            if cert.file_path is None:
                logger.error("Certificate %s has no file_path", cert.id)
                continue

            abs_path = os.path.join(storage_dir, cert.file_path)
            if not os.path.exists(abs_path):
                logger.error("Certificate file missing: %s", abs_path)
                continue

            zf.write(abs_path, entry_name)

    return buf.getvalue()
