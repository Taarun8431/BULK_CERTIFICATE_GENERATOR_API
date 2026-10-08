# Interview Notes — Bulk Certificate Generator

15 likely interview questions with concise answers about this codebase, plus "how would you change it if…" guides.

---

## Q1: Why did you choose 202 Accepted and background processing?

**Answer:** Generating a PDF per recipient takes time. If we had 500 recipients and did it synchronously, the HTTP request would hang for minutes — well past any proxy or client timeout. `202 Accepted` returns immediately with a `job_id`; the client polls for progress. The background task runs in FastAPI's thread-pool (sync function), so it doesn't block the event loop. The trade-off is that a server crash loses in-progress jobs, which we mitigate with startup recovery that marks them FAILED.

---

## Q2: What happens if the server restarts while a job is being processed?

**Answer:** The job is stuck in `PROCESSING` status in the database. On the next startup, `recover_interrupted_jobs` is called (in the `lifespan` function). It finds all jobs in `PENDING` or `PROCESSING` state, marks all their non-final certificates as `FAILED` (with "interrupted by server restart"), recomputes the job's final status from the resulting certificate counts (→ `FAILED` or `COMPLETED_WITH_ERRORS` if some were already done), and sets `completed_at`. The client will then see a terminal status when they poll.

---

## Q3: Why do you validate recipients individually instead of rejecting the whole request?

**Answer:** Imagine a coordinator submitting 500 names and one person has a typo in their email. Rejecting all 500 and making them fix one entry is terrible UX. Instead, valid recipients become `PENDING` certificate rows, invalid ones become `INVALID` rows with an error message. The client gets a full report. The only case we reject the whole request is if ALL recipients are invalid — there's nothing useful to do.

---

## Q4: Why are progress counts derived from certificate rows instead of stored counters?

**Answer:** Stored counters drift. If a commit fails halfway, the counter says N but there are N-1 rows. If a crash occurs, the counter is never decremented. Derived counts (one `GROUP BY status` query) are always exactly correct by definition — they reflect the actual state of the database. The tiny extra query cost is negligible.

---

## Q5: Why do you commit after every certificate instead of once at the end?

**Answer:** Two reasons:
1. **Visibility:** The polling client can see real-time progress ("8 of 10 completed") rather than seeing 0% then jumping to 100%.
2. **Crash safety:** A crash mid-batch loses at most one in-flight certificate. All others are already committed and safe. Committing at the end would lose all progress on crash.

---

## Q6: Why does the background task open its own DB session?

**Answer:** The request's DB session is closed when the HTTP response is sent — before the background task runs. Using the closed session would cause errors. Opening a new session from `session_factory` is the correct pattern. This is also why the processor depends on `session_factory` (not a `Session`): it needs to create its own session at the right time, not be handed one that may be closed.

---

## Q7: How are PDFs written atomically? Why?

**Answer:** 
1. Write bytes to a temp file in the same directory as the destination (same filesystem = same device, required for atomic rename).
2. `os.replace(tmp_path, dest_path)` — on POSIX this is an atomic rename. On Windows, it's best-effort (the old file is replaced only after the new one is fully written).

**Why:** If the process crashes during a normal `open(path, 'wb'); f.write(bytes)`, you get a corrupt partial PDF at the destination. With the rename approach, either the old file remains or the new complete file is present — never a partial.

---

## Q8: Why are file names server-generated UUIDs?

**Answer:** Path traversal attack prevention. If a certificate were named after user input (e.g., `../../../etc/passwd.pdf`), an attacker could write files to arbitrary locations. Server-generated UUIDs have no relationship to user input and are unpredictable.

---

## Q9: How would you swap BackgroundTasks for Celery?

**Answer:**
1. Add `celery` and `redis` to requirements.
2. Create a Celery app: `celery_app = Celery("app", broker="redis://localhost:6379/0")`.
3. Decorate `process_job` with `@celery_app.task`.
4. In the route, replace:
   ```python
   background_tasks.add_task(process_job, job_id, factory, settings, generator)
   ```
   with:
   ```python
   process_job.delay(job_id)
   ```
   (Celery tasks can't take SQLAlchemy objects, so `process_job` would reconstruct `factory` and `generator` from settings it reads itself.)

No other changes needed in the rest of the codebase.

---

## Q10: How would you switch from SQLite to PostgreSQL?

**Answer:** Set one environment variable:
```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/certificates_db"
```
That's it. SQLAlchemy handles all dialect differences. You'd also want to:
- Add `psycopg2-binary` to `requirements.txt`.
- Remove the SQLite-specific pragmas (they're guarded by `if url.startswith("sqlite")`).

---

## Q11: How would you add a new field to the certificate (e.g., a participant's company)?

**Answer:**
1. Add `company: Optional[str]` to the `Certificate` model in `models.py`.
2. Add `company: Optional[str]` to `RecipientIn` schema in `schemas.py`.
3. Pass `company` from the request recipient to the `Certificate` row in `job_service.py`.
4. Add `company` to `CertificateData` dataclass in `certificate_generator.py`.
5. Render it in `PdfCertificateGenerator.generate()`.
6. Update `JobCreateRequest` to accept the new field (or make it per-recipient).
7. Write a migration (add Alembic, or `Base.metadata.create_all` won't add columns to existing tables — that's a known limitation currently).

---

## Q12: How does the processor handle an unexpected crash?

**Answer:** The outer try/except in `process_job` catches any exception that escapes `_process_job_inner`. It then calls `_mark_job_failed` which:
- Marks all remaining PENDING certificates as FAILED with "job aborted".
- Sets the job status to FAILED with an error message.
- Commits.

Then it returns cleanly (does not re-raise). FastAPI's background task runner never sees an exception — it would just log it otherwise.

---

## Q13: How would you add retry of failed certificates?

**Answer:**
1. Add `POST /api/v1/jobs/{job_id}/retry-failed` route.
2. In the service: set FAILED certificates back to PENDING, set job back to PROCESSING.
3. Schedule `process_job(job_id, ...)` as a background task.
4. The processor already only processes PENDING certificates, so it picks up the retried ones.

---

## Q14: How would you add email delivery of certificates?

**Answer:**
1. Add `email_recipient` boolean field to `JobCreateRequest`.
2. After a certificate is generated, call `send_email(cert.recipient_email, pdf_bytes)`.
3. Use `smtplib` (stdlib) or `fastapi-mail`.
4. Store `emailed_at` on the Certificate row.
5. Log failures without crashing the generation loop (same try/except pattern).

---

## Q15: How would you add authentication?

**Answer:**
1. Add `python-jose` + `passlib` for JWT, or a simple API key header.
2. Create a FastAPI `Security` dependency: `api_key_header = APIKeyHeader(name="X-API-Key")`.
3. Add it to routes: `def submit_job(..., api_key: str = Depends(verify_api_key))`.
4. Store valid API keys in the DB or env var.
5. No changes to business logic needed — authentication is entirely in the dependency layer.

---

## How would you change it if…

### …the batch size grows to 100,000 recipients?

Replace BackgroundTasks with Celery + Redis. Stream certificate rows to the DB in batches of 100 to avoid huge transactions. Use a database connection pool (PostgreSQL + pgbouncer). Add rate limiting on the POST endpoint.

### …you need multiple certificate templates?

1. Add a `template_name` field to `JobCreateRequest` and the `Job` model.
2. Create a `TemplateRegistry` dict: `{"default": PdfCertificateGenerator(), "diploma": DiplomaPdfGenerator()}`.
3. The processor picks the right generator from the registry based on `job.template_name`.
4. Store template designs as JSON or HTML+Jinja2 in the DB for a template editor.

### …you need a CSV upload alternative for recipients?

1. Add `POST /api/v1/jobs/upload-csv` that accepts `multipart/form-data` with a CSV file.
2. Parse with `csv.DictReader`.
3. Build the same `recipients: list[dict]` structure and call the same `create_job` service function.

### …you need to support async generation (e.g., 10 concurrent PDF generations)?

ReportLab is CPU-bound and thread-safe. Use `asyncio.to_thread(generator.generate, data)` in an async version of the processor, or use `concurrent.futures.ThreadPoolExecutor` with `max_workers=10`.
