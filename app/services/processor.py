"""
processor.py – Background job processor and startup recovery.

process_job:
  - Opens its own DB session (never reuses the request session).
  - Processes PENDING certificates one by one inside try/except so one
    failure never stops others.
  - Commits after every certificate so progress is visible immediately
    and a crash loses at most one in-flight certificate.
  - Computes final job status from counts after the loop.
  - Outer try/except catches unexpected errors, marks remaining PENDING
    certificates FAILED, and marks the job FAILED – then returns cleanly
    (never raises into FastAPI's background-task runner).

recover_interrupted_jobs:
  - Called once at startup to clean up jobs that were PENDING or PROCESSING
    when the server last died mid-run.
  - Marks such jobs (and their non-final certificates) as FAILED with
    "interrupted by server restart".

Both functions open their own sessions via session_factory.
"""

from __future__ import annotations

import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.services.certificate_generator import (
    CertificateData,
    CertificateGenerator,
    write_pdf_atomically,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_certificate_number() -> str:
    """Generate a unique certificate number: CERT-<YEAR>-<8 hex chars>."""
    import secrets
    year = datetime.now(timezone.utc).year
    return f"CERT-{year}-{secrets.token_hex(4).upper()}"


def _compute_job_status(db: Session, job_id: str) -> str:
    """Derive the final job status from its certificates' statuses.

    Rules (from spec 2.6):
    - All COMPLETED → COMPLETED
    - At least one COMPLETED, at least one FAILED/INVALID → COMPLETED_WITH_ERRORS
    - None COMPLETED → FAILED
    """
    from sqlalchemy import func
    rows = db.execute(
        select(Certificate.status, func.count(Certificate.id).label("cnt"))
        .where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()

    status_counts = {r.status: r.cnt for r in rows}
    n_completed = status_counts.get(CertificateStatus.COMPLETED.value, 0)
    n_failed = status_counts.get(CertificateStatus.FAILED.value, 0)
    n_invalid = status_counts.get(CertificateStatus.INVALID.value, 0)

    if n_completed > 0 and (n_failed + n_invalid) == 0:
        return JobStatus.COMPLETED.value
    elif n_completed > 0:
        return JobStatus.COMPLETED_WITH_ERRORS.value
    else:
        return JobStatus.FAILED.value


def process_job(
    job_id: str,
    session_factory: sessionmaker[Session],
    settings: Settings,
    generator: CertificateGenerator,
) -> None:
    """Process all PENDING certificates for a job.

    This function is designed to be run as a FastAPI BackgroundTask.
    It opens its own session and must never raise – all errors are caught
    and persisted to the DB.
    """
    db: Optional[Session] = None
    try:
        db = session_factory()
        _process_job_inner(db, job_id, settings, generator)
    except Exception:
        # Unexpected error outside the per-certificate loop (e.g. DB gone).
        logger.exception("Unexpected error in process_job for job_id=%s", job_id)
        if db is not None:
            try:
                _mark_job_failed(db, job_id, "Processor encountered an unexpected error")
            except Exception:
                logger.exception("Failed to mark job %s as FAILED", job_id)
    finally:
        if db is not None:
            db.close()


def _process_job_inner(
    db: Session,
    job_id: str,
    settings: Settings,
    generator: CertificateGenerator,
) -> None:
    """Inner implementation: run inside a session, can raise."""
    job = db.get(Job, job_id)
    if job is None:
        logger.error("process_job: job %s not found", job_id)
        return

    # Mark job as PROCESSING.
    job.status = JobStatus.PROCESSING.value
    job.started_at = _utcnow()
    db.commit()

    logger.info("Job %s started processing", job_id)

    # Load all PENDING certificates ordered by row_index.
    pending_certs = db.execute(
        select(Certificate)
        .where(
            Certificate.job_id == job_id,
            Certificate.status == CertificateStatus.PENDING.value,
        )
        .order_by(Certificate.row_index)
    ).scalars().all()

    # Storage directory for this job's PDFs.
    job_storage_dir = os.path.join(
        settings.storage_dir, "certificates", job_id
    )

    for cert in pending_certs:
        try:
            cert_number = _new_certificate_number()
            cert.certificate_number = cert_number

            data = CertificateData(
                recipient_name=cert.recipient_name or "",
                event_name=job.event_name,
                organization=job.organization,
                description=job.description or "for successfully completing",
                issue_date=job.issue_date,
                certificate_number=cert_number,
                signatory_name=job.signatory_name,
                signatory_title=job.signatory_title,
            )

            pdf_bytes = generator.generate(data)

            # Relative path stored in DB (relative to storage_dir root).
            rel_path = os.path.join(
                "certificates", job_id, f"{cert.id}.pdf"
            )
            abs_path = os.path.join(settings.storage_dir, rel_path)
            write_pdf_atomically(pdf_bytes, abs_path)

            cert.status = CertificateStatus.COMPLETED.value
            cert.file_path = rel_path
            cert.generated_at = _utcnow()

            db.commit()
            logger.debug("Certificate %s generated for %s", cert.id, cert.recipient_name)

        except Exception as exc:
            # Roll back the current (failed) certificate changes.
            db.rollback()

            # Clear any partial state that was set before the error.
            # Re-fetch the cert since rollback reverted in-memory changes.
            db.expire(cert)
            cert_fresh = db.get(Certificate, cert.id)
            if cert_fresh is not None:
                cert_fresh.status = CertificateStatus.FAILED.value
                # client-safe message: class name only, full traceback in logs.
                cert_fresh.error_message = (
                    f"generation failed: {type(exc).__name__}"
                )

            logger.error(
                "Certificate %s failed for %s: %s",
                cert.id,
                cert.recipient_name,
                exc,
                exc_info=True,
            )

            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Failed to persist FAILED status for certificate %s", cert.id
                )

    # Compute and persist final job status.
    job_fresh = db.get(Job, job_id)
    if job_fresh is not None:
        final_status = _compute_job_status(db, job_id)
        job_fresh.status = final_status
        job_fresh.completed_at = _utcnow()
        db.commit()

    logger.info("Job %s finished with status %s", job_id, final_status if job_fresh else "unknown")


def _mark_job_failed(db: Session, job_id: str, reason: str) -> None:
    """Mark a job and all its PENDING certificates as FAILED."""
    try:
        pending_certs = db.execute(
            select(Certificate).where(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.PENDING.value,
            )
        ).scalars().all()

        for cert in pending_certs:
            cert.status = CertificateStatus.FAILED.value
            cert.error_message = "job aborted"

        job = db.get(Job, job_id)
        if job is not None:
            job.status = JobStatus.FAILED.value
            job.error_message = reason
            job.completed_at = _utcnow()

        db.commit()
    except Exception:
        db.rollback()
        raise


def recover_interrupted_jobs(
    session_factory: sessionmaker[Session],
) -> None:
    """Mark jobs that were PENDING/PROCESSING when the server crashed.

    Called once at startup.  For each interrupted job:
    - Mark all non-final certificates FAILED with "interrupted by server restart".
    - Compute the job's final status from the resulting certificate counts.
    - Set completed_at.

    This is "fail-cleanly" recovery (not re-queuing).  Re-queuing would
    require a persistent queue (Celery+Redis) and is documented as future
    scope.
    """
    db = session_factory()
    try:
        interrupted = db.execute(
            select(Job).where(
                Job.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value])
            )
        ).scalars().all()

        if not interrupted:
            return

        for job in interrupted:
            logger.warning(
                "Startup recovery: job %s was in status %s – marking as failed",
                job.id, job.status,
            )

            # Mark non-final certificates as FAILED.
            non_final_certs = db.execute(
                select(Certificate).where(
                    Certificate.job_id == job.id,
                    Certificate.status.in_(
                        [CertificateStatus.PENDING.value]
                    ),
                )
            ).scalars().all()

            for cert in non_final_certs:
                cert.status = CertificateStatus.FAILED.value
                cert.error_message = "interrupted by server restart"

            db.flush()

            # Recompute status from what's now in DB.
            final_status = _compute_job_status(db, job.id)
            job.status = final_status
            job.error_message = "interrupted by server restart"
            job.completed_at = _utcnow()

        db.commit()
        logger.info("Startup recovery complete: %d job(s) recovered", len(interrupted))

    except Exception:
        db.rollback()
        logger.exception("Error during startup recovery")
    finally:
        db.close()
