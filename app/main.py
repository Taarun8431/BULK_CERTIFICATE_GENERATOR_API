"""
main.py – FastAPI application factory with lifespan management.

Startup (lifespan):
1. Configure logging.
2. Create DB tables (idempotent with create_all).
3. Create STORAGE_DIR/certificates (exist_ok=True).
4. Run startup recovery (mark interrupted jobs as failed).

Error handlers:
- NotFoundError → 404
- ConflictError → 409
- ValidationError (our own) → 422
- FastAPI RequestValidationError → 422 (consistent {"detail","errors"} shape)
- Catch-all → 500 (no stack trace to client)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import setup_logging
from app.database import Base, build_engine, build_session_factory
from app.services.processor import recover_interrupted_jobs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Application lifespan: startup and shutdown."""
    settings = get_settings()
    setup_logging(settings)

    # 1. Create DB tables (no-op if already present → no Alembic needed).
    engine = build_engine(settings)
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ensured")

    # 2. Create storage directory structure.
    certs_dir = os.path.join(settings.storage_dir, "certificates")
    os.makedirs(certs_dir, exist_ok=True)
    logger.info("Storage directory ensured: %s", certs_dir)

    # 3. Startup recovery: clean up any jobs that died mid-run.
    factory = build_session_factory(settings)
    recover_interrupted_jobs(factory)

    yield
    # Shutdown – nothing to clean up (SQLite closes connections on GC).
    logger.info("Application shutdown")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="Bulk Certificate Generator",
        description=(
            "API for generating PDF certificates in bulk for events and courses."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    # -----------------------------------------------------------------------
    # Exception handlers – map our domain errors to HTTP status codes.
    # -----------------------------------------------------------------------

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": exc.message, "errors": []},
        )

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": exc.message, "errors": []},
        )

    @app.exception_handler(ValidationError)
    async def validation_error_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": exc.message, "errors": []},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Return FastAPI validation errors in our consistent {"detail","errors"} shape."""
        errors = [
            {
                "field": " → ".join(str(loc) for loc in e["loc"]),
                "message": e["msg"],
                "type": e["type"],
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Request validation failed",
                "errors": errors,
            },
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all: log full traceback; return generic 500 to client."""
        logger.exception("Unhandled exception on %s %s", request.method, request.url)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "errors": []},
        )

    # -----------------------------------------------------------------------
    # Routers
    # -----------------------------------------------------------------------

    from app.api import health, jobs, certificates

    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(certificates.router)

    return app


# Module-level app instance used by uvicorn.
app = create_app()
