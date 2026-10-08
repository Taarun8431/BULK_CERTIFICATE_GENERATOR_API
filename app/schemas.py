"""
schemas.py – Pydantic v2 request/response models.

Design decisions:
- The top-level POST /api/v1/jobs request uses `recipients: list[Any]` so
  that individual invalid entries do NOT cause a Pydantic rejection of the
  whole request.  Per-recipient validation is done inside JobService using
  RecipientIn, and invalid ones become INVALID certificate rows.
- All response models use `model_config = ConfigDict(from_attributes=True)`
  so they can be constructed from ORM model instances via model_validate().
- Datetimes are serialized as ISO-8601 strings (Pydantic v2 default).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class RecipientIn(BaseModel):
    """Schema used to validate one recipient entry inside JobService.

    Validation rules (per spec 2.6):
    - name: string, trimmed, 1..MAX_NAME_LENGTH chars, no control chars.
    - email: valid email (via email-validator), normalized to lowercase.
    - Extra fields are silently ignored.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    email: EmailStr

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, v: Any) -> str:
        """Strip whitespace; reject blank or control-character values."""
        if not isinstance(v, str):
            raise ValueError("name must be a string")
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        # Reject control characters (0x00-0x1F except normal whitespace)
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", v):
            raise ValueError("name contains invalid control characters")
        return v

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: Any) -> Any:
        """Lowercase the email before EmailStr validation."""
        if isinstance(v, str):
            return v.strip().lower()
        return v


class JobCreateRequest(BaseModel):
    """Body of POST /api/v1/jobs.

    Recipients are typed as list[Any] so Pydantic does not reject the whole
    request when individual items are malformed; validation happens per-item
    inside JobService.
    """

    model_config = ConfigDict(extra="ignore")

    event_name: str = Field(..., description="Name of the event or course")
    organization: str = Field(..., description="Organizing body name")
    description: Optional[str] = Field(
        None, description="Completion description line on the certificate"
    )
    issue_date: Optional[date] = Field(
        None, description="Issue date (ISO 8601 date); defaults to today (UTC)"
    )
    signatory_name: Optional[str] = Field(None, description="Name of the signatory")
    signatory_title: Optional[str] = Field(None, description="Title of the signatory")

    # list[Any] – per-item validation done manually in JobService.
    recipients: list[Any] = Field(..., description="List of recipient objects")

    @field_validator("event_name", "organization", mode="before")
    @classmethod
    def trim_and_require(cls, v: Any) -> str:
        """Strip whitespace and reject blank/non-string values."""
        if not isinstance(v, str):
            raise ValueError("must be a non-empty string")
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("description", "signatory_name", "signatory_title", mode="before")
    @classmethod
    def trim_optional(cls, v: Any) -> Optional[str]:
        """Strip whitespace from optional string fields; keep None as None."""
        if v is None:
            return None
        if isinstance(v, str):
            return v.strip() or None
        return v


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class JobCreateResponse(BaseModel):
    """Returned from POST /api/v1/jobs (202 Accepted)."""

    job_id: str
    status: str
    total: int
    accepted: int
    rejected: int
    status_url: str


class FailureItem(BaseModel):
    """A single failed or invalid certificate in the job-status response."""

    certificate_id: str
    row_index: int
    recipient_name: Optional[str]
    recipient_email: Optional[str]
    status: str
    error_message: Optional[str]


class JobStatusResponse(BaseModel):
    """Returned from GET /api/v1/jobs/{job_id}."""

    model_config = ConfigDict(from_attributes=True)

    job_id: str
    status: str
    event_name: str
    organization: str
    total: int
    counts: dict[str, int]
    processed: int
    progress_percent: float
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error_message: Optional[str]
    failures: list[FailureItem]
    download_url: str


class CertificateItem(BaseModel):
    """One row in the paginated certificates list."""

    certificate_id: str
    row_index: int
    recipient_name: Optional[str]
    recipient_email: Optional[str]
    status: str
    certificate_number: Optional[str]
    error_message: Optional[str]
    download_url: Optional[str]


class CertificateListResponse(BaseModel):
    """Returned from GET /api/v1/jobs/{job_id}/certificates."""

    job_id: str
    total: int
    limit: int
    offset: int
    items: list[CertificateItem]


class HealthResponse(BaseModel):
    """Returned from GET /health."""

    status: str
