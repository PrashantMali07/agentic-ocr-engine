"""
SQLite-backed store for async extraction jobs.

Shared across processes on one machine and persists across restarts, with
no extra service to run (unlike Redis). Fine for a single-server
deployment; each job is stored as a JSON blob keyed by job_id, so this
file is a drop-in replacement for the Redis-backed JobStore - same
create/get/update/delete interface, so api.py needs no changes beyond
the import.
"""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from .config import DEFAULT_OUTPUT_DIR
from .schema import ExtractResponse, JobProgress, JobStatus, JobStatusResponse, ReviewRequest
from .security.auth import Principal

log = logging.getLogger("ingest.jobs")

DB_PATH = Path(DEFAULT_OUTPUT_DIR) / "jobs.db"

# The worker polling loop enforces these TTLs after artifact cleanup.
JOB_TTL = timedelta(hours=24)
REVIEW_TTL = timedelta(days=7)

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tenant_id TEXT,
    owner_id TEXT,
    status TEXT,
    reserved_bytes INTEGER NOT NULL DEFAULT 0,
    manifest TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
"""

ACTIVE_STATES = (JobStatus.queued.value, JobStatus.processing.value)
def _bounded_capacity(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


# Tune these only from representative release-host measurements. The hard
# upper bounds prevent a typo from disabling admission control entirely.
MAX_PENDING_JOBS = _bounded_capacity("INGESTION_MAX_PENDING_JOBS", 8, 64)
MAX_TENANT_JOBS = _bounded_capacity("INGESTION_MAX_TENANT_JOBS", 4, 64)
MAX_PENDING_BYTES = _bounded_capacity("INGESTION_MAX_PENDING_BYTES", 120 * 1024 * 1024, 1024 * 1024 * 1024)
MAX_TENANT_BYTES = _bounded_capacity("INGESTION_MAX_TENANT_BYTES", 60 * 1024 * 1024, 1024 * 1024 * 1024)
MAX_RUNNING_JOBS = 1
MAX_ATTEMPTS = 2
MAX_SUBMISSIONS_PER_MINUTE = _bounded_capacity("INGESTION_MAX_SUBMISSIONS_PER_MINUTE", 100, 1000)
MAX_TENANT_SUBMISSIONS_PER_MINUTE = _bounded_capacity("INGESTION_MAX_TENANT_SUBMISSIONS_PER_MINUTE", 20, 1000)

if MAX_TENANT_JOBS > MAX_PENDING_JOBS or MAX_TENANT_BYTES > MAX_PENDING_BYTES:
    raise ValueError("Per-tenant queue limits cannot exceed global queue limits")
if MAX_TENANT_SUBMISSIONS_PER_MINUTE > MAX_SUBMISSIONS_PER_MINUTE:
    raise ValueError("Per-tenant submission rate cannot exceed the global rate")


class QueueFull(Exception):
    """Durable queue capacity is exhausted."""


class ReviewConflict(Exception):
    """Review is stale or does not match the provisional result."""


class JobStore:
    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite serializes writes anyway; this just avoids concurrent
        # "database is locked" errors within this one process.
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(_INIT_SQL)
                async with db.execute("PRAGMA table_info(jobs)") as cur:
                    columns = {row[1] for row in await cur.fetchall()}
                if "tenant_id" not in columns:
                    await db.execute("ALTER TABLE jobs ADD COLUMN tenant_id TEXT")
                if "owner_id" not in columns:
                    await db.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT")
                extra = {
                    "status": "TEXT",
                    "reserved_bytes": "INTEGER NOT NULL DEFAULT 0",
                    "manifest": "TEXT",
                    "lease_owner": "TEXT",
                    "lease_until": "TEXT",
                    "attempts": "INTEGER NOT NULL DEFAULT 0",
                    # Phase 02: one scheduling authority per job. Existing rows
                    # (pre-migration) are preserved as legacy and remain claimable
                    # by the legacy worker exactly as before.
                    "scheduler_kind": "TEXT",
                }
                for name, sql_type in extra.items():
                    if name not in columns:
                        await db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
                if "scheduler_kind" not in columns:
                    # Unknown-owner pre-migration rows stay with the legacy worker.
                    await db.execute("UPDATE jobs SET scheduler_kind = 'legacy' WHERE scheduler_kind IS NULL")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs (tenant_id, owner_id, job_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (status, lease_until, updated_at)")
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_scheduler ON jobs (scheduler_kind, status, updated_at)"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS admission_events (tenant_id TEXT NOT NULL, admitted_at TEXT NOT NULL)"
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_admission_time ON admission_events (admitted_at)")
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS worker_state (name TEXT PRIMARY KEY, last_seen TEXT NOT NULL)"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS review_events ("
                    "job_id TEXT NOT NULL, version INTEGER NOT NULL, tenant_id TEXT NOT NULL, "
                    "reviewer_id TEXT NOT NULL, reviewed_at TEXT NOT NULL, request TEXT NOT NULL, "
                    "before_result TEXT NOT NULL, after_result TEXT NOT NULL, "
                    "PRIMARY KEY (job_id, version))"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS job_observations ("
                    "job_id TEXT NOT NULL, attempt INTEGER NOT NULL, duration_seconds REAL NOT NULL, "
                    "peak_rss_bytes INTEGER, peak_gpu_bytes INTEGER, peak_artifact_bytes INTEGER, "
                    "exit_kind TEXT NOT NULL, "
                    "recorded_at TEXT NOT NULL, PRIMARY KEY (job_id, attempt))"
                )
                async with db.execute("PRAGMA table_info(job_observations)") as cur:
                    observation_columns = {row[1] for row in await cur.fetchall()}
                if "peak_artifact_bytes" not in observation_columns:
                    await db.execute("ALTER TABLE job_observations ADD COLUMN peak_artifact_bytes INTEGER")
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS maintenance_counters ("
                    "name TEXT PRIMARY KEY, count INTEGER NOT NULL, last_at TEXT NOT NULL)"
                )
                # Phase 02: transactional outbox for test-lane submission. A
                # row records the admitted job + validated request packet so
                # a reconciler can (re)start the resolved identical workflow
                # after any crash, without trusting process-local memory.
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS lane_submissions ("
                    "job_id TEXT PRIMARY KEY, scheduler_kind TEXT NOT NULL, "
                    "request TEXT NOT NULL, submitted_at TEXT NOT NULL, "
                    "workflow_id TEXT, last_reconcile_at TEXT NOT NULL)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_lane_submissions_pending "
                    "ON lane_submissions (scheduler_kind, last_reconcile_at)"
                )
                async with db.execute("PRAGMA table_info(lane_submissions)") as cur:
                    lane_columns = {row[1] for row in await cur.fetchall()}
                if "review_signal_version" not in lane_columns:
                    await db.execute("ALTER TABLE lane_submissions ADD COLUMN review_signal_version INTEGER NOT NULL DEFAULT 0")
                if "cancel_delivered_at" not in lane_columns:
                    await db.execute("ALTER TABLE lane_submissions ADD COLUMN cancel_delivered_at TEXT")
                await db.commit()
            self._initialized = True

    async def create(self, job_id: str, principal: Principal) -> JobStatusResponse:
        await self._ensure_init()
        now = datetime.now(timezone.utc)
        job = JobStatusResponse(
            job_id=job_id,
            tenant_id=principal.tenant_id,
            owner_id=principal.subject,
            status=JobStatus.queued,
            created_at=now,
            updated_at=now,
        )
        await self._write(job)
        return job

    async def admit(self, job_id: str, principal: Principal, manifest: list[dict], reserved_bytes: int,
                    *, scheduler_kind: str = "legacy", lane_request: Optional[dict] = None) -> JobStatusResponse:
        """Atomically reserve capacity, persist a job, and stage its lane intent."""
        await self._ensure_init()
        if not manifest or reserved_bytes <= 0:
            raise ValueError("A job requires nonempty stored input")
        if scheduler_kind not in {"legacy", "temporal_test"}:
            raise ValueError("Unknown job scheduler")
        if (scheduler_kind == "temporal_test") != (lane_request is not None):
            raise ValueError("Temporal jobs require an outbox request in the admission transaction")
        if lane_request is not None and lane_request.get("job_id") != job_id:
            raise ValueError("Outbox request does not match the admitted job")
        now = datetime.now(timezone.utc)
        job = JobStatusResponse(
            job_id=job_id, tenant_id=principal.tenant_id, owner_id=principal.subject,
            status=JobStatus.queued, created_at=now, updated_at=now,
        )
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cutoff = (now - timedelta(minutes=1)).isoformat()
                await db.execute("DELETE FROM admission_events WHERE admitted_at < ?", (cutoff,))
                async with db.execute("SELECT COUNT(*) FROM admission_events") as cur:
                    recent_global = (await cur.fetchone())[0]
                async with db.execute(
                    "SELECT COUNT(*) FROM admission_events WHERE tenant_id = ?", (principal.tenant_id,)
                ) as cur:
                    recent_tenant = (await cur.fetchone())[0]
                async with db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(reserved_bytes), 0) FROM jobs "
                    "WHERE status IN (?, ?) AND manifest IS NOT NULL",
                    ACTIVE_STATES,
                ) as cur:
                    global_count, global_bytes = await cur.fetchone()
                async with db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(reserved_bytes), 0) FROM jobs "
                    "WHERE status IN (?, ?) AND tenant_id = ? AND manifest IS NOT NULL",
                    (*ACTIVE_STATES, principal.tenant_id),
                ) as cur:
                    tenant_count, tenant_bytes = await cur.fetchone()
                if (global_count >= MAX_PENDING_JOBS or tenant_count >= MAX_TENANT_JOBS
                        or global_bytes + reserved_bytes > MAX_PENDING_BYTES
                        or tenant_bytes + reserved_bytes > MAX_TENANT_BYTES
                        or recent_global >= MAX_SUBMISSIONS_PER_MINUTE
                        or recent_tenant >= MAX_TENANT_SUBMISSIONS_PER_MINUTE):
                    raise QueueFull
                await db.execute(
                    "INSERT INTO jobs (job_id, data, updated_at, tenant_id, owner_id, status, reserved_bytes, manifest, scheduler_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, job.model_dump_json(), now.isoformat(), principal.tenant_id,
                     principal.subject, JobStatus.queued.value, reserved_bytes, json.dumps(manifest), scheduler_kind),
                )
                if lane_request is not None:
                    await db.execute(
                        "INSERT INTO lane_submissions (job_id, scheduler_kind, request, submitted_at, last_reconcile_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (job_id, scheduler_kind, json.dumps(lane_request), now.isoformat(), now.isoformat()),
                    )
                await db.execute(
                    "INSERT INTO admission_events (tenant_id, admitted_at) VALUES (?, ?)",
                    (principal.tenant_id, now.isoformat()),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return job

    # ------------------------------------------------------------------
    # Phase 02 lane submission (outbox) + delivery
    # ------------------------------------------------------------------

    async def pending_lane_submissions(self, scheduler_kind: str, limit: int = 25) -> list[dict]:
        """Outbox rows for a scheduler kind that still need a workflow start."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT lane_submissions.job_id, lane_submissions.request FROM lane_submissions "
                "JOIN jobs ON jobs.job_id = lane_submissions.job_id "
                "WHERE lane_submissions.scheduler_kind = ? AND jobs.status = 'queued' "
                "AND (workflow_id IS NULL OR workflow_id = '') "
                "LIMIT ?",
                (scheduler_kind, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"job_id": row[0], "request": json.loads(row[1])} for row in rows]

    async def submission_for_job(self, job_id: str) -> Optional[dict]:
        """Outbox row for a job, or None."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT job_id, scheduler_kind, request, workflow_id,"
                " submitted_at, last_reconcile_at FROM lane_submissions WHERE job_id = ?",
                (job_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return {"job_id": row[0], "scheduler_kind": row[1], "request": json.loads(row[2]),
                "workflow_id": row[3], "submitted_at": row[4], "last_reconcile_at": row[5]}

    async def submitted_lane_jobs(self, scheduler_kind: str, limit: int = 25) -> list[dict]:
        """Outbox rows whose workflow has already been started."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT lane_submissions.job_id, lane_submissions.request FROM lane_submissions "
                "JOIN jobs ON jobs.job_id = lane_submissions.job_id "
                "WHERE lane_submissions.scheduler_kind = ? AND jobs.status IN ('queued', 'processing') "
                "AND workflow_id IS NOT NULL AND workflow_id != '' "
                "LIMIT ?",
                (scheduler_kind, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"job_id": row[0], "request": json.loads(row[1])} for row in rows]

    async def waiting_lane_jobs(self, scheduler_kind: str, limit: int = 25) -> list[str]:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT lane_submissions.job_id FROM lane_submissions "
                "JOIN jobs ON jobs.job_id = lane_submissions.job_id "
                "WHERE lane_submissions.scheduler_kind = ? AND jobs.status = 'waiting_for_review' "
                "AND workflow_id IS NOT NULL LIMIT ?", (scheduler_kind, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def close_failed_lane_review(self, job_id: str, scheduler_kind: str,
                                       *, timed_out: bool) -> bool:
        """Preserve the provisional result while closing a failed review wait."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT data FROM jobs WHERE job_id = ? AND scheduler_kind = ? AND status = ?",
                    (job_id, scheduler_kind, JobStatus.waiting_for_review.value),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return False
                now = datetime.now(timezone.utc)
                status = JobStatus.timed_out if timed_out else JobStatus.failed
                job = JobStatusResponse.model_validate_json(row[0]).model_copy(update={
                    "status": status,
                    "error": "Human review deadline elapsed" if timed_out else "Review confirmation failed",
                    "updated_at": now,
                })
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ? WHERE job_id = ?",
                    (job.model_dump_json(), status.value, now.isoformat(), job_id),
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def record_lane_submission(self, job_id: str, workflow_id: str) -> None:
        """Persist the workflow identity so reconciliation is idempotent."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE lane_submissions SET workflow_id = ?, last_reconcile_at = ? "
                "WHERE job_id = ?",
                (workflow_id, datetime.now(timezone.utc).isoformat(), job_id),
            )
            await db.commit()

    async def pending_lane_reviews(self, scheduler_kind: str, limit: int = 25) -> list[dict]:
        """Accepted reviews whose bounded workflow signal needs delivery."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT lane_submissions.job_id, lane_submissions.workflow_id, "
                "review_events.version, review_events.request FROM lane_submissions "
                "JOIN review_events ON review_events.job_id = lane_submissions.job_id "
                "JOIN jobs ON jobs.job_id = lane_submissions.job_id "
                "WHERE lane_submissions.scheduler_kind = ? AND jobs.status IN "
                "('waiting_for_review', 'completed', 'partial') AND workflow_id IS NOT NULL "
                "AND review_events.version > lane_submissions.review_signal_version "
                "ORDER BY review_events.reviewed_at LIMIT ?", (scheduler_kind, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"job_id": row[0], "workflow_id": row[1], "version": row[2],
                 "request": json.loads(row[3])} for row in rows]

    async def record_lane_review_signal(self, job_id: str, version: int) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE lane_submissions SET review_signal_version = MAX(review_signal_version, ?) "
                "WHERE job_id = ?", (version, job_id),
            )
            await db.commit()

    async def pending_lane_cancellations(self, scheduler_kind: str, limit: int = 25) -> list[dict]:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT lane_submissions.job_id, workflow_id FROM lane_submissions "
                "JOIN jobs ON jobs.job_id = lane_submissions.job_id "
                "WHERE lane_submissions.scheduler_kind = ? AND jobs.status = 'cancelled' "
                "AND workflow_id IS NOT NULL AND cancel_delivered_at IS NULL LIMIT ?",
                (scheduler_kind, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [{"job_id": row[0], "workflow_id": row[1]} for row in rows]

    async def record_lane_cancellation(self, job_id: str) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE lane_submissions SET cancel_delivered_at = ? WHERE job_id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            await db.commit()

    async def set_processing_by_scheduler(self, job_id: str, scheduler_kind: str) -> bool:
        """Mark an admitted job processing (guard: scheduler kind must match)."""
        await self._ensure_init()
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT data FROM jobs WHERE job_id = ? AND scheduler_kind = ? AND status = ?",
                    (job_id, scheduler_kind, JobStatus.queued.value),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return False
                job = JobStatusResponse.model_validate_json(row[0]).model_copy(update={
                    "status": JobStatus.processing, "updated_at": now,
                })
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ? WHERE job_id = ?",
                    (job.model_dump_json(), JobStatus.processing.value, now.isoformat(), job_id),
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def deliver_workflow_result(
        self, job_id: str, scheduler_kind: str, result_payload: dict,
        *, needs_review: bool = False,
    ) -> bool:
        """Write one idempotent workflow aggregate into the job record.

        Guarded by scheduler kind, so a legacy job can never be overwritten
        by a test-lane delivery (or vice versa). Repeated delivery with the
        same payload is a no-op; different content on the same version is a
        conflict. Review status comes from the workflow's page audit, not
        from this method's caller.
        """
        await self._ensure_init()
        now = datetime.now(timezone.utc)
        status = (
            JobStatus.failed if result_payload.get("status") == "failed"
            else JobStatus.waiting_for_review if needs_review
            else JobStatus.partial if result_payload.get("status") == "partial"
            else JobStatus.completed
        )
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT data, status FROM jobs WHERE job_id = ? AND scheduler_kind = ?",
                    (job_id, scheduler_kind),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return False
                job = JobStatusResponse.model_validate_json(row[0])
                job_status = row[1]
                if job_status == JobStatus.waiting_for_review.value:
                    # Human review already happened upstream; never overwrite it.
                    await db.commit()
                    return False
                if job_status in {JobStatus.completed.value, JobStatus.partial.value, JobStatus.failed.value}:
                    await db.commit()
                    return False
                if job_status in {JobStatus.cancelled.value, JobStatus.timed_out.value}:
                    await db.commit()
                    return False
                job = job.model_copy(update={
                    "status": status,
                    "result": ExtractResponse.model_validate(result_payload),
                    "error": result_payload.get("error"),
                    "updated_at": now,
                    "progress": JobProgress(total=1, processed=1),
                })
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ? WHERE job_id = ?",
                    (job.model_dump_json(), status.value, now.isoformat(), job_id),
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def claim_next(self, worker_id: str, lease_seconds: int = 30) -> tuple[str, list[dict], int] | None:
        """Claim one queued job or expired lease under a SQLite write transaction."""
        await self._ensure_init()
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status = ? AND lease_until > ?",
                    (JobStatus.processing.value, now.isoformat()),
                ) as cur:
                    running = (await cur.fetchone())[0]
                if running >= MAX_RUNNING_JOBS:
                    await db.commit()
                    return None
                async with db.execute(
                    "SELECT job_id, data, manifest, attempts FROM jobs WHERE manifest IS NOT NULL "
                    "AND (scheduler_kind IS NULL OR scheduler_kind = 'legacy') "
                    "AND ((status = ?) OR (status = ? AND lease_until <= ?)) "
                    "AND attempts < ? ORDER BY updated_at LIMIT 1",
                    (JobStatus.queued.value, JobStatus.processing.value, now.isoformat(), MAX_ATTEMPTS),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return None
                job_id, data, manifest, attempts = row
                job = JobStatusResponse.model_validate_json(data).model_copy(update={
                    "status": JobStatus.processing, "updated_at": now,
                    "progress": JobProgress(total=len(json.loads(manifest)), processed=0),
                })
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ?, lease_owner = ?, "
                    "lease_until = ?, attempts = ? WHERE job_id = ?",
                    (job.model_dump_json(), JobStatus.processing.value, now.isoformat(),
                     worker_id, lease_until, attempts + 1, job_id),
                )
                await db.commit()
                return job_id, json.loads(manifest), attempts + 1
            except BaseException:
                await db.rollback()
                raise

    async def renew_lease(self, job_id: str, worker_id: str, lease_seconds: int = 30) -> bool:
        await self._ensure_init()
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE jobs SET lease_until = ? WHERE job_id = ? AND status = ? AND lease_owner = ?",
                (deadline, job_id, JobStatus.processing.value, worker_id),
            )
            if cur.rowcount == 1:
                await db.execute(
                    "INSERT INTO worker_state (name, last_seen) VALUES ('last', ?) "
                    "ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen",
                    (datetime.now(timezone.utc).isoformat(),),
                )
            await db.commit()
            return cur.rowcount == 1

    async def touch_worker(self) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO worker_state (name, last_seen) VALUES ('last', ?) "
                "ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await db.commit()

    async def record_job_observation(self, job_id: str, attempt: int, duration_seconds: float,
                                     peak_rss_bytes: int | None, peak_gpu_bytes: int | None,
                                     exit_kind: str, peak_artifact_bytes: int | None = None) -> None:
        """Persist non-document process measurements for each worker attempt."""
        await self._ensure_init()
        if attempt < 1 or duration_seconds < 0 or exit_kind not in {
            "completed", "failed", "timed_out", "cancelled", "sigkill", "oom"
        }:
            raise ValueError("Invalid job observation")
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO job_observations (job_id, attempt, duration_seconds, peak_rss_bytes, "
                "peak_gpu_bytes, peak_artifact_bytes, exit_kind, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, attempt) DO NOTHING",
                (job_id, attempt, duration_seconds, peak_rss_bytes, peak_gpu_bytes, peak_artifact_bytes,
                 exit_kind, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def record_cleanup_failure(self) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO maintenance_counters (name, count, last_at) VALUES ('artifact_cleanup', 1, ?) "
                "ON CONFLICT(name) DO UPDATE SET count = count + 1, last_at = excluded.last_at",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await db.commit()

    async def operational_snapshot(self) -> dict:
        """Aggregate non-document operational data for readiness and metrics."""
        await self._ensure_init()
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT status, COUNT(*), COALESCE(SUM(reserved_bytes), 0) "
                                  "FROM jobs GROUP BY status") as cur:
                states = await cur.fetchall()
            async with db.execute(
                "SELECT COUNT(*), COALESCE(SUM(reserved_bytes), 0) FROM jobs "
                "WHERE status IN (?, ?) AND manifest IS NOT NULL", ACTIVE_STATES,
            ) as cur:
                active_count, active_reserved = await cur.fetchone()
            async with db.execute("SELECT MIN(updated_at) FROM jobs WHERE status = ?",
                                  (JobStatus.queued.value,)) as cur:
                queue_oldest = (await cur.fetchone())[0]
            async with db.execute("SELECT MIN(updated_at) FROM jobs WHERE status = ?",
                                  (JobStatus.waiting_for_review.value,)) as cur:
                review_oldest = (await cur.fetchone())[0]
            async with db.execute("SELECT MIN(updated_at) FROM jobs WHERE status = ?",
                                  (JobStatus.processing.value,)) as cur:
                processing_oldest = (await cur.fetchone())[0]
            async with db.execute("SELECT COALESCE(SUM(MAX(attempts - 1, 0)), 0) FROM jobs") as cur:
                retries = (await cur.fetchone())[0]
            async with db.execute("SELECT last_seen FROM worker_state WHERE name = 'last'") as cur:
                row = await cur.fetchone()
            async with db.execute(
                "SELECT exit_kind, COUNT(*), MAX(duration_seconds), MAX(peak_rss_bytes), "
                "MAX(peak_gpu_bytes), MAX(peak_artifact_bytes) "
                "FROM job_observations GROUP BY exit_kind"
            ) as cur:
                observations = await cur.fetchall()
            async with db.execute(
                "SELECT count, last_at FROM maintenance_counters WHERE name = 'artifact_cleanup'"
            ) as cur:
                cleanup = await cur.fetchone()
            async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE manifest IS NOT NULL AND "
                "status IN (?, ?, ?, ?, ?, ?)",
                (JobStatus.waiting_for_review.value, JobStatus.completed.value,
                 JobStatus.partial.value, JobStatus.failed.value,
                 JobStatus.cancelled.value, JobStatus.timed_out.value),
            ) as cur:
                pending_cleanup = (await cur.fetchone())[0]
        worker_seen = row[0] if row else None

        def age(value):
            return max(0.0, (now - datetime.fromisoformat(value)).total_seconds()) if value else None

        disk = shutil.disk_usage(self._db_path.parent)
        counts = {status: count for status, count, _ in states}
        queue_age = age(queue_oldest)
        review_age = age(review_oldest)
        worker_age = age(worker_seen)
        reserved = active_reserved
        alerts = []
        if worker_age is None or worker_age > 10:
            alerts.append("worker_stale")
        if queue_age is not None and queue_age > 300:
            alerts.append("queue_stuck")
        if review_age is not None and review_age > 24 * 3600:
            alerts.append("review_overdue")
        processing_age = age(processing_oldest)
        if processing_age is not None and processing_age > 21 * 60:
            alerts.append("processing_stuck")
        if active_count >= MAX_PENDING_JOBS or reserved >= MAX_PENDING_BYTES:
            alerts.append("queue_capacity")
        if disk.free < 256 * 1024 * 1024:
            alerts.append("disk_low")
        if cleanup and pending_cleanup and age(cleanup[1]) is not None and age(cleanup[1]) < 300:
            alerts.append("artifact_cleanup_failed")
        return {
            "job_counts": counts,
            "reserved_bytes": reserved,
            "queue_oldest_seconds": queue_age,
            "review_oldest_seconds": review_age,
            "processing_oldest_seconds": processing_age,
            "worker_last_seen_seconds": worker_age,
            "retry_attempts": retries,
            "disk_free_bytes": disk.free,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "attempt_counts": {kind: count for kind, count, *_ in observations},
            "max_duration_seconds": max((duration for _, _, duration, *_ in observations), default=None),
            "peak_rss_bytes": max((row[3] for row in observations if row[3] is not None), default=None),
            "peak_gpu_bytes": max((row[4] for row in observations if row[4] is not None), default=None),
            "peak_artifact_bytes": max((row[5] for row in observations if row[5] is not None), default=None),
            "cleanup_failure_count": cleanup[0] if cleanup else 0,
            "pending_cleanup_jobs": pending_cleanup,
            "alerts": alerts,
        }

    async def manifest_for_worker(self, job_id: str, worker_id: str) -> list[dict] | None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT manifest FROM jobs WHERE job_id = ? AND status = ? AND lease_owner = ?",
                (job_id, JobStatus.processing.value, worker_id),
            ) as cur:
                row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def fail_exhausted_leases(self) -> int:
        """After a worker crash, terminally fail leases with no retries left."""
        await self._ensure_init()
        now = datetime.now(timezone.utc)
        changed = 0
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT job_id, data FROM jobs WHERE status = ? AND lease_until <= ? AND attempts >= ?",
                    (JobStatus.processing.value, now.isoformat(), MAX_ATTEMPTS),
                ) as cur:
                    rows = await cur.fetchall()
                for job_id, data in rows:
                    job = JobStatusResponse.model_validate_json(data).model_copy(update={
                        "status": JobStatus.failed, "error": "Worker stopped before job completion",
                        "updated_at": now,
                    })
                    await db.execute(
                        "UPDATE jobs SET data = ?, status = ?, updated_at = ?, lease_owner = NULL, "
                        "lease_until = NULL WHERE job_id = ?",
                        (job.model_dump_json(), JobStatus.failed.value, now.isoformat(), job_id),
                    )
                    changed += 1
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return changed

    async def transition(self, job_id: str, worker_id: str, status: JobStatus,
                         *, result=None, error: str | None = None) -> bool:
        """Commit a terminal state only for the worker still holding the lease."""
        await self._ensure_init()
        if status not in {JobStatus.waiting_for_review, JobStatus.completed, JobStatus.partial,
                          JobStatus.failed, JobStatus.timed_out}:
            raise ValueError("Invalid worker transition")
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT data FROM jobs WHERE job_id = ? AND status = ? AND lease_owner = ?",
                    (job_id, JobStatus.processing.value, worker_id),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return False
                job = JobStatusResponse.model_validate_json(row[0]).model_copy(update={
                    "status": status, "result": result, "error": error, "updated_at": now,
                })
                if status in {JobStatus.waiting_for_review, JobStatus.completed, JobStatus.partial} and job.progress:
                    job = job.model_copy(update={
                        "progress": JobProgress(total=job.progress.total, processed=job.progress.total),
                    })
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ?, lease_owner = NULL, "
                    "lease_until = NULL WHERE job_id = ?",
                    (job.model_dump_json(), status.value, now.isoformat(), job_id),
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def release_or_fail(self, job_id: str, worker_id: str, error: str = "Document processing failed") -> bool:
        """Requeue a failed attempt, or record a terminal failure after max attempts."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT data, attempts FROM jobs WHERE job_id = ? AND status = ? AND lease_owner = ?",
                    (job_id, JobStatus.processing.value, worker_id),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return False
                now = datetime.now(timezone.utc)
                status = JobStatus.failed if row[1] >= MAX_ATTEMPTS else JobStatus.queued
                job = JobStatusResponse.model_validate_json(row[0]).model_copy(update={
                    "status": status, "error": error if status == JobStatus.failed else None,
                    "updated_at": now,
                })
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ?, lease_owner = NULL, "
                    "lease_until = NULL WHERE job_id = ?",
                    (job.model_dump_json(), status.value, now.isoformat(), job_id),
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def cancel(self, job_id: str, principal: Principal) -> bool:
        """Mark an owned active job cancelled; worker observes and kills its child."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT data FROM jobs WHERE job_id = ? AND tenant_id = ? AND owner_id = ? "
                    "AND status IN (?, ?, ?)",
                    (job_id, principal.tenant_id, principal.subject,
                     *ACTIVE_STATES, JobStatus.waiting_for_review.value),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return False
                now = datetime.now(timezone.utc)
                job = JobStatusResponse.model_validate_json(row[0]).model_copy(update={
                    "status": JobStatus.cancelled, "updated_at": now,
                })
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ? WHERE job_id = ?",
                    (job.model_dump_json(), JobStatus.cancelled.value, now.isoformat(), job_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return True

    async def terminal_artifact_jobs(self, limit: int = 100) -> list[str]:
        """Find finished jobs whose local input/output artifacts need removal."""
        await self._ensure_init()
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT job_id FROM jobs WHERE manifest IS NOT NULL AND "
                "status IN (?, ?, ?, ?, ?, ?) AND (lease_owner IS NULL OR lease_until <= ?) "
                "ORDER BY updated_at LIMIT ?",
                (JobStatus.waiting_for_review.value, JobStatus.completed.value, JobStatus.partial.value,
                 JobStatus.failed.value, JobStatus.cancelled.value, JobStatus.timed_out.value, now, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def release_cancelled_lease(self, job_id: str, worker_id: str) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE jobs SET lease_owner = NULL, lease_until = NULL WHERE job_id = ? "
                "AND status = ? AND lease_owner = ?",
                (job_id, JobStatus.cancelled.value, worker_id),
            )
            await db.commit()

    async def mark_artifacts_cleaned(self, job_id: str) -> None:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE jobs SET manifest = NULL, reserved_bytes = 0 WHERE job_id = ? "
                "AND status IN (?, ?, ?, ?, ?, ?)",
                (job_id, JobStatus.waiting_for_review.value, JobStatus.completed.value,
                 JobStatus.partial.value, JobStatus.failed.value, JobStatus.cancelled.value,
                 JobStatus.timed_out.value),
            )
            await db.commit()

    async def get(self, job_id: str) -> Optional[JobStatusResponse]:
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT data FROM jobs WHERE job_id = ?", (job_id,)) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return JobStatusResponse.model_validate_json(row[0])

    async def get_for_principal(self, job_id: str, principal: Principal) -> Optional[JobStatusResponse]:
        await self._ensure_init()
        reviewer = bool({"review", "admin"} & principal.roles)
        query = (
            "SELECT data FROM jobs WHERE job_id = ? AND tenant_id = ?"
            if reviewer else
            "SELECT data FROM jobs WHERE job_id = ? AND tenant_id = ? AND owner_id = ?"
        )
        args = (job_id, principal.tenant_id) if reviewer else (job_id, principal.tenant_id, principal.subject)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(query, args) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        job = JobStatusResponse.model_validate_json(row[0])
        return job if not self._expired(job) else None

    @staticmethod
    def _expired(job: JobStatusResponse, now: datetime | None = None) -> bool:
        if job.status in {JobStatus.queued, JobStatus.processing}:
            return False
        ttl = REVIEW_TTL if job.status == JobStatus.waiting_for_review else JOB_TTL
        return job.updated_at < (now or datetime.now(timezone.utc)) - ttl

    async def submit_review(self, job_id: str, principal: Principal,
                            request: ReviewRequest) -> Optional[JobStatusResponse]:
        """Atomically apply exact-span corrections and retain the before/after audit."""
        if not {"review", "admin"} & principal.roles:
            raise PermissionError("Review role required")
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT data FROM jobs WHERE job_id = ? AND tenant_id = ?",
                    (job_id, principal.tenant_id),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.commit()
                    return None
                job = JobStatusResponse.model_validate_json(row[0])
                if self._expired(job):
                    await db.commit()
                    return None
                if job.status != JobStatus.waiting_for_review or job.review_version != request.result_version:
                    raise ReviewConflict("Review state or result version changed")
                if job.result is None or request.document_name not in job.result.documents:
                    raise ReviewConflict("Document is absent from the provisional result")
                before = job.result
                document = before.documents[request.document_name]
                if request.corrections and (document.html_content is not None or document.tables is not None):
                    raise ReviewConflict("Structured table content cannot be corrected through text offsets")
                content = document.content
                previous_end = 0
                for correction in sorted(request.corrections, key=lambda item: item.start):
                    if (correction.start < previous_end or correction.end > len(content) or
                            content[correction.start:correction.end] != correction.expected):
                        raise ReviewConflict("Correction span is overlapping, stale, or outside the document")
                    # Content can contain sanitized HTML tables. Never allow an offset to alter markup.
                    if "<" in correction.expected or ">" in correction.expected or "&" in correction.expected:
                        raise ReviewConflict("Correction cannot replace HTML markup or entities")
                    if content.rfind("<", 0, correction.start) > content.rfind(">", 0, correction.start):
                        raise ReviewConflict("Correction starts inside HTML markup")
                    previous_end = correction.end
                for correction in sorted(request.corrections, key=lambda item: item.start, reverse=True):
                    content = content[:correction.start] + correction.replacement + content[correction.end:]
                audit = dict(document.audit or {})
                if audit.get("requires_hitl") is True:
                    audit["requires_hitl"] = False
                if audit.get("needs_review") is True:
                    audit["needs_review"] = False
                audit["reviewed"] = True
                updated_doc = document.model_copy(update={
                    "content": content, "correction_suggestions": None, "audit": audit,
                })
                documents = dict(before.documents)
                documents[request.document_name] = updated_doc
                after = ExtractResponse.model_validate(before.model_copy(update={"documents": documents}))
                if len(after.model_dump_json().encode("utf-8")) > 8 * 1024 * 1024:
                    raise ReviewConflict("Reviewed result exceeds the supported limit")
                now = datetime.now(timezone.utc)
                pending = any(
                    doc.correction_suggestions or
                    (doc.audit and (doc.audit.get("requires_hitl") is True or doc.audit.get("needs_review") is True))
                    for doc in documents.values()
                )
                final_status = (JobStatus.waiting_for_review if pending else
                                JobStatus.partial if any(doc.status == "partial" for doc in documents.values())
                                else JobStatus.completed)
                updated = job.model_copy(update={
                    "status": final_status, "result": after, "review_version": job.review_version + 1,
                    "reviewed_at": now, "reviewed_by": principal.subject, "updated_at": now,
                })
                await db.execute(
                    "INSERT INTO review_events (job_id, version, tenant_id, reviewer_id, reviewed_at, "
                    "request, before_result, after_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, updated.review_version, principal.tenant_id, principal.subject,
                     now.isoformat(), request.model_dump_json(), before.model_dump_json(), after.model_dump_json()),
                )
                await db.execute(
                    "UPDATE jobs SET data = ?, status = ?, updated_at = ? WHERE job_id = ?",
                    (updated.model_dump_json(), final_status.value, now.isoformat(), job_id),
                )
                await db.commit()
                # The reconciler delivers the committed event from review_events.
                # API latency never depends on a Temporal network round trip.
                return updated
            except BaseException:
                await db.rollback()
                raise

    async def review_history(self, job_id: str, principal: Principal) -> Optional[list[dict]]:
        """Return review requests and metadata; full snapshots remain in SQLite."""
        if not {"review", "admin"} & principal.roles:
            raise PermissionError("Review role required")
        job = await self.get_for_principal(job_id, principal)
        if job is None:
            return None
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT version, reviewer_id, reviewed_at, request FROM review_events "
                "WHERE job_id = ? AND tenant_id = ? ORDER BY version",
                (job_id, principal.tenant_id),
            ) as cur:
                rows = await cur.fetchall()
        return [{"version": row[0], "reviewed_by": row[1], "reviewed_at": row[2],
                 "request": json.loads(row[3])} for row in rows]

    async def update(self, job_id: str, **fields) -> Optional[JobStatusResponse]:
        """Partial update - only pass the fields that changed."""
        job = await self.get(job_id)
        if job is None:
            log.warning("Tried to update unknown/expired job %s", job_id)
            return None
        updated = job.model_copy(update={**fields, "updated_at": datetime.now(timezone.utc)})
        await self._write(updated)
        return updated

    async def delete(self, job_id: str) -> bool:
        await self._ensure_init()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("DELETE FROM review_events WHERE job_id = ?", (job_id,))
                await db.execute("DELETE FROM job_observations WHERE job_id = ?", (job_id,))
                cur = await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                await db.commit()
                return cur.rowcount > 0

    async def purge_expired(self, ttl: timedelta = JOB_TTL,
                            review_ttl: timedelta = REVIEW_TTL) -> int:
        """Delete expired finished jobs only after their artifacts are removed."""
        await self._ensure_init()
        cutoff = (datetime.now(timezone.utc) - ttl).isoformat()
        review_cutoff = (datetime.now(timezone.utc) - review_ttl).isoformat()
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                conditions = (
                    "manifest IS NULL AND ((status IN (?, ?, ?, ?, ?) AND updated_at < ?) "
                    "OR (status = ? AND updated_at < ?))"
                )
                args = (JobStatus.completed.value, JobStatus.partial.value, JobStatus.failed.value,
                        JobStatus.cancelled.value, JobStatus.timed_out.value, cutoff,
                        JobStatus.waiting_for_review.value, review_cutoff)
                await db.execute(
                    f"DELETE FROM review_events WHERE job_id IN (SELECT job_id FROM jobs WHERE {conditions})",
                    args,
                )
                await db.execute(
                    f"DELETE FROM job_observations WHERE job_id IN (SELECT job_id FROM jobs WHERE {conditions})",
                    args,
                )
                cur = await db.execute(f"DELETE FROM jobs WHERE {conditions}", args)
                await db.commit()
                return cur.rowcount

    async def _write(self, job: JobStatusResponse) -> None:
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "INSERT INTO jobs (job_id, data, updated_at, tenant_id, owner_id, status) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET data = excluded.data, "
                    "updated_at = excluded.updated_at, tenant_id = excluded.tenant_id, "
                    "owner_id = excluded.owner_id, status = excluded.status",
                    (job.job_id, job.model_dump_json(), job.updated_at.isoformat(), job.tenant_id,
                     job.owner_id, job.status.value),
                )
                await db.commit()


# Single shared instance for the app.
job_store = JobStore()
