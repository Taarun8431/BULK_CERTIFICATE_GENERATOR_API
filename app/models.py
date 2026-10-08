"""
models.py – SQLAlchemy 2.0 ORM models using Mapped/mapped_column.

Two tables:
  jobs          – one row per certificate-generation request.
  certificates  – one row per recipient (valid or invalid).

Status enums are stored as plain strings so the DB stays human-readable
and migrations are simpler than native DB enums.
"""

import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enums (Python-side; stored as VARCHAR in the DB)
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class CertificateStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INVALID = "INVALID"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _uuid() -> str:
    """Generate a new UUID4 string (used as default PK factory)."""
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------

class Job(Base):
    """One row per certificate-generation request submitted by the client."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # Event details (from the request body)
    event_name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    signatory_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    signatory_title: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Status tracking
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=JobStatus.PENDING.value
    )
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps (all UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationship: one job → many certificates (cascade delete)
    certificates: Mapped[list["Certificate"]] = relationship(
        "Certificate",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Certificate.row_index",
    )

    def __repr__(self) -> str:
        return f"<Job id={self.id!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# Certificate model
# ---------------------------------------------------------------------------

class Certificate(Base):
    """One row per recipient submitted in a job request."""

    __tablename__ = "certificates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Position in the original recipients list (0-based)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Recipient info (null for invalid rows where parsing failed entirely)
    recipient_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    recipient_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    # Stored so INVALID rows can be reported back to the client verbatim
    raw_input: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CertificateStatus.PENDING.value
    )

    # Assigned when generation begins; globally unique per spec
    certificate_number: Mapped[Optional[str]] = mapped_column(
        String(40), nullable=True, unique=True
    )

    # Relative path under STORAGE_DIR; set once the PDF is on disk
    file_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Why the certificate is INVALID or FAILED
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    generated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationship back to job
    job: Mapped["Job"] = relationship("Job", back_populates="certificates")

    def __repr__(self) -> str:
        return (
            f"<Certificate id={self.id!r} status={self.status!r} "
            f"name={self.recipient_name!r}>"
        )


# ---------------------------------------------------------------------------
# Composite index for efficient status queries within a job
# ---------------------------------------------------------------------------

_cert_job_status_idx = Index(
    "ix_certificates_job_id_status",
    Certificate.job_id,
    Certificate.status,
)
