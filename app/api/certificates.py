"""
certificates.py – /api/v1/certificates routes.

Routes:
  GET /api/v1/certificates/{certificate_id}/download → PDF file (200)
    - 404 if unknown
    - 409 if PENDING/FAILED/INVALID
    - 404 (with log) if DB says COMPLETED but file is missing
"""

import logging
import os

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.models import Certificate, CertificateStatus
from app.core.exceptions import ConflictError, NotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/certificates")


def _get_db(factory: sessionmaker[Session] = Depends(get_session_factory)) -> Session:
    db = factory()
    try:
        yield db
    finally:
        db.close()


@router.get("/{certificate_id}/download")
def download_certificate(
    certificate_id: str,
    db: Session = Depends(_get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Download a single certificate PDF.

    Status codes:
    - 200: PDF returned with Content-Disposition attachment.
    - 404: certificate not found or file missing.
    - 409: certificate is not in COMPLETED status.
    """
    cert = db.get(Certificate, certificate_id)

    if cert is None:
        raise NotFoundError(f"Certificate '{certificate_id}' not found")

    if cert.status != CertificateStatus.COMPLETED.value:
        raise ConflictError(
            f"Certificate is {cert.status}, not yet available for download"
        )

    if cert.file_path is None:
        logger.error("Certificate %s has no file_path despite COMPLETED status", certificate_id)
        raise NotFoundError("Certificate file not found")

    abs_path = os.path.join(settings.storage_dir, cert.file_path)

    if not os.path.exists(abs_path):
        logger.error(
            "Certificate file missing on disk: %s (certificate_id=%s)",
            abs_path,
            certificate_id,
        )
        raise NotFoundError("Certificate file not found on disk")

    filename = f"certificate_{cert.certificate_number}.pdf"
    return FileResponse(
        path=abs_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
