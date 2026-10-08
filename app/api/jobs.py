"""
jobs.py – /api/v1/jobs routes.

Routes (thin layer – all business logic delegated to job_service):
  POST /api/v1/jobs                             → 202 JobCreateResponse
  GET  /api/v1/jobs/{job_id}                    → 200 JobStatusResponse
  GET  /api/v1/jobs/{job_id}/certificates       → 200 CertificateListResponse
  GET  /api/v1/jobs/{job_id}/download           → ZIP

All route functions are plain `def` so FastAPI runs them in a thread-pool,
preventing them from blocking the event loop.
"""

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.database import get_session_factory
from app.models import Certificate, Job
from app.schemas import (
    CertificateListResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobStatusResponse,
)
from app.services.certificate_generator import CertificateGenerator, get_generator
from app.services.job_service import (
    build_zip,
    create_job,
    get_job_summary,
    list_certificates,
)
from app.services.processor import process_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs")


def _get_db(factory: sessionmaker[Session] = Depends(get_session_factory)) -> Session:
    """Open one DB session for the duration of the request."""
    db = factory()
    try:
        yield db
    finally:
        db.close()


@router.post("", status_code=202)
def submit_job(
    payload: JobCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(_get_db),
    settings: Settings = Depends(get_settings),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    generator: CertificateGenerator = Depends(get_generator),
) -> JobCreateResponse:
    """Accept a certificate generation request; schedule background processing.

    Returns 202 immediately.  The job is created synchronously so the
    client gets a job_id in the response; generation happens in the background.

    Validates max_recipients_per_request here (request-level) since Pydantic
    cannot access settings at schema parse time.
    """
    if len(payload.recipients) == 0:
        raise ValidationError("recipients list must not be empty")

    if len(payload.recipients) > settings.max_recipients_per_request:
        raise ValidationError(
            f"Too many recipients: {len(payload.recipients)} exceeds "
            f"the maximum of {settings.max_recipients_per_request}"
        )

    result = create_job(db, payload, settings)

    background_tasks.add_task(
        process_job,
        result["job_id"],
        factory,
        settings,
        generator,
    )

    return JobCreateResponse(
        job_id=result["job_id"],
        status=result["status"],
        total=result["total"],
        accepted=result["accepted"],
        rejected=result["rejected"],
        status_url=f"/api/v1/jobs/{result['job_id']}",
    )


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job(
    job_id: str,
    db: Session = Depends(_get_db),
) -> JobStatusResponse:
    """Return current status and progress counts for a job."""
    return get_job_summary(db, job_id)


@router.get("/{job_id}/certificates", response_model=CertificateListResponse)
def get_job_certificates(
    job_id: str,
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(_get_db),
) -> CertificateListResponse:
    """Paginated list of certificates for a job, with optional status filter."""
    return list_certificates(db, job_id, status, limit, offset)


@router.get("/{job_id}/download")
def download_job_zip(
    job_id: str,
    db: Session = Depends(_get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Download a ZIP archive of all COMPLETED certificates for a job."""
    zip_bytes = build_zip(db, job_id, settings.storage_dir)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="certificates_{job_id}.zip"'
        },
    )
