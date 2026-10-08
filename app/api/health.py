"""
health.py – GET /health endpoint.

Performs a trivial DB query to verify the DB is reachable.
Returns 200 {"status": "ok"} or 503 {"status": "degraded"}.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health_check(
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> JSONResponse:
    """Check application health including DB connectivity."""
    try:
        db = factory()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        logger.error("Health check DB query failed: %s", exc)
        return JSONResponse({"status": "degraded"}, status_code=503)
