# Design Decisions

This document records every significant decision made during implementation, with alternatives considered, the chosen option, and the trade-offs.

---

## 1. Processing Model: FastAPI BackgroundTasks vs Synchronous vs Celery+Redis

| | Synchronous | **BackgroundTasks (chosen)** | Celery + Redis |
|---|---|---|---|
| Returns immediately | ❌ | ✅ | ✅ |
| Survives server restart | N/A | ❌ | ✅ |
| Extra infrastructure | None | None | Redis + Worker |
| Complexity | Very low | Low | Medium-High |
| Swappable | N/A | Yes (one line) | Yes |

**Decision:** FastAPI `BackgroundTasks`.

**Why:** The spec says "no extra infrastructure." The only real trade-off is crash recovery, which is mitigated by startup recovery (fail-cleanly on next boot). The entire scheduling logic is in one line: `background_tasks.add_task(process_job, ...)` — changing to Celery means replacing that line with `process_job.delay(...)`.

---

## 2. FastAPI vs Django+DRF vs Flask

**Decision:** FastAPI.

**Why:**
- Native Pydantic v2 integration for request/response validation.
- `BackgroundTasks` built-in, no extra library.
- Automatic OpenAPI/Swagger docs from type hints.
- Async-capable (even though we use sync handlers for this project).
- Type hints throughout keep the code self-documenting.

**Flask:** Would work, but no built-in validation, docs, or background tasks.
**Django+DRF:** Heavy for a standalone API; adds ORM, admin, migrations we don't need.

---

## 3. SQLite vs PostgreSQL

**Decision:** SQLite by default, PostgreSQL via `DATABASE_URL` env var.

**Why:** SQLite needs zero setup for development and the interview demo. The `DATABASE_URL` config knob means switching to PostgreSQL is a one-variable change with zero code changes. SQLAlchemy abstracts all differences.

**Trade-off:** SQLite WAL handles moderate concurrency; under very high load PostgreSQL is required.

---

## 4. Per-Recipient Validation vs Reject-All

**Decision:** Validate each recipient independently; invalid ones become `INVALID` certificate rows.

**Why:** A batch of 500 recipients shouldn't fail because one person has a typo in their email. The client gets a full report of which recipients were accepted and which were rejected, with reasons.

**Alternative:** Reject the entire request. Simple, but terrible UX for large batches.

**When we reject the whole request:** If ALL recipients are invalid (nothing useful to do) → 422 immediately, no job created.

---

## 5. Derived Counts vs Stored Counters

**Decision:** Counts (`pending`, `completed`, `failed`, `invalid`) are derived from a `GROUP BY` query on certificate rows at read time.

**Why:** Stored counters can drift if a commit fails, a crash occurs, or if we update certificates without remembering to update the counter. Derived counts are always accurate by definition.

**Trade-off:** One extra query per status check (negligible for SQLite/Postgres at this scale).

---

## 6. Commit-Per-Certificate

**Decision:** Commit to the database after every certificate is processed (not at the end of the batch).

**Why:**
1. Progress is immediately visible to the polling client.
2. A crash loses at most one in-flight certificate — all others are safely persisted.

**Alternative:** Commit once at the end. Simpler, but loses all progress on crash.

---

## 7. PDF via ReportLab

**Decision:** ReportLab canvas, landscape A4, built-in Helvetica fonts, `pageCompression=0`.

**Why:** ReportLab is the de-facto standard Python PDF library. No external font files needed (Helvetica is built in). `pageCompression=0` keeps text uncompressed so tests can search the raw bytes.

**Known limitation:** Helvetica covers ISO-8859-1 only. Non-latin characters are replaced with `?`. Future: embed DejaVu or Noto font.

---

## 8. Atomic File Writes

**Decision:** Write PDF to a temp file (same directory), then `os.replace()` to the final path.

**Why:** A crash during a normal write leaves a corrupt half-written file. `os.replace()` is atomic on POSIX (rename) and best-effort on Windows — either the old file remains or the new file is present, never a partial file.

---

## 9. Server-Generated File Names

**Decision:** Certificate files are named `<certificate_id>.pdf` (UUID4), not anything derived from user input.

**Why:** Path traversal prevention. Never use user-supplied data in file paths. The UUID is generated server-side.

---

## 10. Startup Recovery: Fail-Cleanly vs Re-Queue

**Decision:** On startup, mark PENDING/PROCESSING jobs as FAILED (fail-cleanly). Do not re-queue.

**Why:** Re-queuing requires a persistent task queue (so the task survives the crash). With BackgroundTasks there is no such queue. Fail-cleanly is honest — the client sees a terminal status and can re-submit.

**Alternative (future scope):** Celery + Redis would allow re-queuing: store the job_id in the queue before starting, consume it in the worker, and let Celery handle retries.

---

## 11. One Transaction for Job + Certificates Creation

**Decision:** Job row and all Certificate rows are persisted in a single transaction.

**Why:** An error between creating the Job and creating Certificate rows would leave an orphan Job with no certificates. One transaction is atomic — either everything is committed or nothing is.

---

## 12. Processor Opens Its Own Session

**Decision:** `process_job` opens its own `Session` from `session_factory`, separate from the request session.

**Why:** The request session is closed when the HTTP response is returned. The background task runs after the response, so reusing the request session would mean using a closed session. Opening a new session is the correct pattern for background work.
