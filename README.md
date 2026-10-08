# Bulk Certificate Generator

## Overview

A production-quality REST API for generating PDF certificates in bulk for events and courses. Submit one request containing many recipients; the API validates each one, generates a personalized PDF certificate from a predefined template, and lets you retrieve them individually or as a ZIP archive.

**Key behaviors:**
- Invalid recipients are recorded and reported, not silently dropped — one bad email does not reject the whole batch.
- Generation runs in the background (returns `202 Accepted` immediately).
- One failing generation does not stop others.
- Progress is visible in real-time via the status endpoint.

---

## Tech Stack

| Component | Library | Version |
|---|---|---|
| Web framework | FastAPI | ≥0.110 |
| ASGI server | Uvicorn | ≥0.29 |
| ORM | SQLAlchemy 2.0 | ≥2.0 |
| Validation | Pydantic v2 | ≥2.5 |
| PDF generation | ReportLab | ≥4.0 |
| Database | SQLite (default) / PostgreSQL | — |
| Testing | pytest + httpx + pypdf | — |
| Python | 3.10, 3.11, 3.12 | — |

---

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 2. Install all dependencies (dev includes prod)
pip install -r requirements-dev.txt
```

No `.env` file is required for local development; all settings have sensible defaults.

To override, set environment variables before starting the server:

```bash
# Windows (PowerShell)
$env:DATABASE_URL = "postgresql://user:pass@localhost:5432/certs"
$env:STORAGE_DIR = "C:\data\certificates"

# macOS / Linux
export DATABASE_URL="postgresql://user:pass@localhost:5432/certs"
export STORAGE_DIR="/var/data/certificates"
```

---

## Running the Application

```bash
# Start development server (auto-creates DB tables and storage directory)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Available URLs:
- **API**: http://localhost:8000/api/v1/jobs
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **Health check**: http://localhost:8000/health

---

## Running Tests

```bash
# Run all tests (verbose output)
pytest -v

# Run all tests (quiet – just pass/fail summary)
pytest -q

# Run a specific test file
pytest tests/test_certificate_generation.py -v

# Run without order dependence confirmation
pytest -q -p no:randomly

# Run a specific test by name
pytest -k "test_pdf_contains_recipient_name" -v
```

All tests use temporary in-memory SQLite databases and temp directories — they never touch `./storage` or `./certificates.db`.

---

## Submitting a Certificate Generation Request

### Using curl (macOS / Linux)

```bash
curl -s -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d @sample_data/sample_request.json | python -m json.tool
```

### Using PowerShell (Windows)

```powershell
$body = Get-Content sample_data\sample_request.json -Raw
$response = Invoke-RestMethod -Uri http://localhost:8000/api/v1/jobs `
  -Method POST -ContentType "application/json" -Body $body
$response | ConvertTo-Json -Depth 5
```

### Payload field reference

| Field | Type | Required | Description |
|---|---|---|---|
| `event_name` | string | **Yes** | Name of the event or course |
| `organization` | string | **Yes** | Organizing body name |
| `description` | string | No | Description line on certificate (default: "for successfully completing") |
| `issue_date` | ISO date string | No | Certificate date (default: today UTC) |
| `signatory_name` | string | No | Name printed on the signature line |
| `signatory_title` | string | No | Title printed under the signature |
| `recipients` | array | **Yes** | List of `{"name": "...", "email": "..."}` objects |

### Recipient validation rules

- `name`: string, 1–100 characters, trimmed, no control characters
- `email`: valid email address, normalized to lowercase
- Duplicate emails within the same request: second occurrence recorded as INVALID
- Non-object entries (integers, strings, null): recorded as INVALID
- Unknown extra fields: silently ignored

### Example response (202 Accepted)

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "total": 5,
  "accepted": 4,
  "rejected": 1,
  "status_url": "/api/v1/jobs/550e8400-e29b-41d4-a716-446655440000"
}
```

---

## Retrieving Generated Certificates

### 1. Poll job status

```bash
curl -s http://localhost:8000/api/v1/jobs/{job_id} | python -m json.tool
```

Wait until `status` is `COMPLETED`, `COMPLETED_WITH_ERRORS`, or `FAILED`.

### 2. List all certificates for a job

```bash
# All certificates
curl -s "http://localhost:8000/api/v1/jobs/{job_id}/certificates"

# Only completed ones
curl -s "http://localhost:8000/api/v1/jobs/{job_id}/certificates?status=COMPLETED"

# Pagination
curl -s "http://localhost:8000/api/v1/jobs/{job_id}/certificates?limit=10&offset=0"
```

### 3. Download a single certificate PDF

```bash
curl -s -o certificate.pdf \
  http://localhost:8000/api/v1/certificates/{certificate_id}/download
```

### 4. Download all completed certificates as a ZIP

```bash
curl -s -o certificates.zip \
  http://localhost:8000/api/v1/jobs/{job_id}/download
```

---

## Important Implementation / Design Decisions

### Processing model: FastAPI BackgroundTasks

**Decision:** Use FastAPI's `BackgroundTasks` for in-process background processing.

**Reasoning:**
- The request returns immediately (`202 Accepted`) so large batches don't time out.
- Zero extra infrastructure — no Redis, no Celery worker, no Docker required.
- Simple to run, deploy, and explain in an interview.
- The background function is synchronous, so it runs in FastAPI's thread-pool — no async complexity.

**Trade-off:** Jobs are lost if the process dies mid-run. **Mitigation:** Startup recovery marks interrupted jobs as `FAILED` so the client always sees a terminal state (no infinite "PROCESSING").

**How to swap to Celery:** Replace the one line `background_tasks.add_task(process_job, ...)` with `process_job.delay(...)` (after defining `process_job` as a Celery task). No other code changes required.

### Per-recipient validation (not reject-all)

Each recipient in the list is validated independently. One bad email does not reject the entire batch. Invalid recipients are stored as `INVALID` certificate rows with a reason, and the client can see which ones failed.

### Derived counts (no counters)

Progress counts (`pending`, `completed`, `failed`, `invalid`) are derived from a `GROUP BY status` query on certificate rows at read time. This is always accurate even after a crash — no counters can drift or get out of sync.

### Commit-per-certificate

The processor commits to the DB after each certificate. This means:
- Progress is visible in real-time (not just at the end).
- A crash loses at most one in-flight certificate — all others are safely persisted.

### Atomic file writes

PDFs are written to a temp file in the same directory, then renamed with `os.replace()`. This ensures no half-written PDFs are ever visible at the destination path.

### Server-generated file names

Certificate files are named `<certificate_id>.pdf` (a UUID). User input never touches the file path. This prevents path traversal attacks.

---

## Project Structure

```
bulk-certificate-generator/
├── app/
│   ├── main.py                 # App factory, lifespan, exception handlers
│   ├── config.py               # Settings dataclass, get_settings()
│   ├── database.py             # Engine/session factory, get_session_factory
│   ├── models.py               # Job, Certificate ORM models + enums
│   ├── schemas.py              # Pydantic request/response models
│   ├── api/
│   │   ├── health.py           # GET /health
│   │   ├── jobs.py             # POST/GET /api/v1/jobs/...
│   │   └── certificates.py     # GET /api/v1/certificates/{id}/download
│   ├── services/
│   │   ├── job_service.py      # create_job, get_job_summary, list, zip
│   │   ├── processor.py        # process_job, recover_interrupted_jobs
│   │   └── certificate_generator.py  # PdfCertificateGenerator + Protocol
│   └── core/
│       ├── exceptions.py       # NotFoundError, ConflictError, ValidationError
│       └── logging.py          # Logging setup
├── tests/
│   ├── conftest.py             # Fixtures, FailingGenerator, helpers
│   ├── test_job_creation.py
│   ├── test_input_validation.py
│   ├── test_certificate_generation.py
│   ├── test_job_status_progress.py
│   ├── test_failure_handling.py
│   ├── test_retrieval.py
│   └── test_recovery_and_health.py
├── docs/                       # Architecture, API, decisions, testing, interview
├── sample_data/                # Sample JSON payloads + generator script
├── README.md
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── .env.example
└── .gitignore
```

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certificates.db` | SQLAlchemy URL; change to `postgresql://...` for Postgres |
| `STORAGE_DIR` | `./storage` | Root folder for generated PDFs |
| `MAX_RECIPIENTS_PER_REQUEST` | `1000` | Request rejected with 422 above this |
| `MAX_NAME_LENGTH` | `100` | Recipient name max length |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Known Limitations

1. **In-process background tasks**: If the server crashes mid-job, that job is marked `FAILED` on next startup (startup recovery). A real task queue (Celery + Redis) would allow retry.
2. **Non-latin characters**: Helvetica (built-in, no font files) only covers ISO-8859-1. Chinese, Arabic, emoji, etc. are replaced with `?` in the PDF. Future fix: embed DejaVu or Noto font.
3. **No authentication**: No API keys or JWT. For production, add an auth middleware layer.
4. **SQLite WAL**: SQLite handles moderate concurrent reads well with WAL mode, but for high-throughput production, switch to PostgreSQL via `DATABASE_URL`.
5. **No retry of FAILED certificates**: Currently the whole job must be re-submitted. A `/retry-failed` endpoint is in the optional extras list.

---

## Future Scope

- Persistent task queue (Celery + Redis) for crash-resilient processing
- Retry FAILED certificates endpoint (`POST /api/v1/jobs/{id}/retry-failed`)
- Multiple certificate templates
- CSV upload for recipients
- Authentication (API key or JWT)
- Email delivery of certificates
- Alembic migrations for schema evolution
- Prometheus metrics endpoint
- Dockerfile + docker-compose.yml
