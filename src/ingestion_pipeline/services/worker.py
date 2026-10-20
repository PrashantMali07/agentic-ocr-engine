"""Single-host SQLite queue consumer with a hard child-process deadline.

Run separately from the API: ``python -m ingestion_pipeline.services.worker``.
Multiple copies may run; SQLite admission and claim transactions enforce one
active OCR process for a shared database. All copies must share the same output
directory. A killed worker's lease is recovered on the next poll.
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
import signal
import sys
import uuid
from pathlib import Path

from ..config import DEFAULT_OUTPUT_DIR
from ..job_store import JobStore, job_store
from ..schema import JobStatus
from .monitoring import artifact_bytes, gpu_memory_bytes, oom_events_path, oom_kill_count, peak_rss_bytes

log = logging.getLogger("ingest.worker")
DEFAULT_JOB_TIMEOUT_SECONDS = 20 * 60
_last_alerts: set[str] = set()


async def log_alert_changes(store: JobStore) -> None:
    """Log each observable alert transition once per worker process."""
    global _last_alerts
    current = set((await store.operational_snapshot())["alerts"])
    for alert in sorted(current - _last_alerts):
        log.warning("Operational alert active: %s", alert)
    for alert in sorted(_last_alerts - current):
        log.info("Operational alert cleared: %s", alert)
    _last_alerts = current


async def cleanup_terminal_artifacts(store: JobStore = job_store) -> None:
    root = Path(DEFAULT_OUTPUT_DIR) / "jobs"
    for job_id in await store.terminal_artifact_jobs():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            log.error("Refusing to clean artifacts for unsafe job identifier")
            continue
        directory = root / job_id
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("Could not clean artifacts for job %s (%s)", job_id, type(exc).__name__)
            await store.record_cleanup_failure()
            continue
        await store.mark_artifacts_cleaned(job_id)


async def _stop_child(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


async def run_once(store: JobStore = job_store, *, timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
                   runner_command: list[str] | None = None) -> bool:
    """Claim and supervise at most one job. Return False when queue is empty."""
    await store.fail_exhausted_leases()
    await cleanup_terminal_artifacts(store)
    await store.purge_expired()
    await store.touch_worker()
    await log_alert_changes(store)
    worker_id = uuid.uuid4().hex
    claimed = await store.claim_next(worker_id)
    if claimed is None:
        return False
    job_id, _, attempt = claimed
    command = runner_command or [sys.executable, "-m", "ingestion_pipeline.services.job_runner"]
    proc = None
    timed_out = False
    lease_lost = False
    started = asyncio.get_running_loop().time()
    peak_rss = None
    peak_gpu = None
    last_gpu_sample = started
    artifact_root = Path(DEFAULT_OUTPUT_DIR) / "jobs" / job_id
    peak_artifacts = artifact_bytes(artifact_root)
    last_artifact_sample = started
    oom_path = None
    initial_oom = None
    try:
        child_env = os.environ.copy()
        child_env.pop("INGESTION_API_KEY_SHA256_JSON", None)
        proc = await asyncio.create_subprocess_exec(
            *command, job_id, worker_id, str(os.getpid()), start_new_session=True,
            env=child_env,
        )
        oom_path = oom_events_path(proc.pid)
        initial_oom = oom_kill_count(oom_path)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while proc.returncode is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                current_rss = peak_rss_bytes(proc.pid)
                if current_rss is not None:
                    peak_rss = max(peak_rss or 0, current_rss)
                if child_env.get("PADDLE_USE_GPU", "true").lower() == "true" and (
                    asyncio.get_running_loop().time() - last_gpu_sample >= 5
                ):
                    current_gpu = await gpu_memory_bytes(proc.pid)
                    if current_gpu is not None:
                        peak_gpu = max(peak_gpu or 0, current_gpu)
                    last_gpu_sample = asyncio.get_running_loop().time()
                if asyncio.get_running_loop().time() - last_artifact_sample >= 5:
                    peak_artifacts = max(peak_artifacts, artifact_bytes(artifact_root))
                    last_artifact_sample = asyncio.get_running_loop().time()
                if not await store.renew_lease(job_id, worker_id):
                    current_job = await store.get(job_id)
                    if current_job and current_job.status in {
                        JobStatus.completed, JobStatus.partial, JobStatus.waiting_for_review,
                        JobStatus.failed, JobStatus.timed_out,
                    }:
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=2)
                        except asyncio.TimeoutError:
                            pass
                    else:
                        lease_lost = True
                    break  # Cancelled or claimed by another worker.
        if proc.returncode is None:
            await _stop_child(proc)
        if timed_out:
            await store.transition(job_id, worker_id, JobStatus.timed_out, error="Document processing timed out")
        elif proc.returncode != 0:
            await store.release_or_fail(job_id, worker_id)
        else:
            # The child commits the result. A clean exit without that commit is a failure.
            job = await store.get(job_id)
            if job and job.status == JobStatus.processing:
                await store.release_or_fail(job_id, worker_id)
        await store.release_cancelled_lease(job_id, worker_id)
        final_job = await store.get(job_id)
        final_status = final_job.status if final_job else None
        if timed_out or final_status == JobStatus.timed_out:
            exit_kind = "timed_out"
        elif final_status in {JobStatus.completed, JobStatus.partial, JobStatus.waiting_for_review}:
            exit_kind = "completed"
        elif lease_lost or final_status == JobStatus.cancelled:
            exit_kind = "cancelled"
        elif proc.returncode == -signal.SIGKILL:
            final_oom = oom_kill_count(oom_path)
            exit_kind = ("oom" if initial_oom is not None and final_oom is not None
                         and final_oom > initial_oom else "sigkill")
        else:
            exit_kind = "failed"
        try:
            peak_artifacts = max(peak_artifacts, artifact_bytes(artifact_root))
            await store.record_job_observation(
                job_id, attempt, asyncio.get_running_loop().time() - started,
                peak_rss, peak_gpu, exit_kind, peak_artifact_bytes=peak_artifacts,
            )
        except Exception as exc:
            log.error("Could not persist job observation (%s)", type(exc).__name__)
        return True
    except BaseException:
        if proc is not None:
            await _stop_child(proc)
        await store.release_or_fail(job_id, worker_id)
        raise


async def run_forever(store: JobStore = job_store, *, timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS) -> None:
    while True:
        worked = await run_once(store, timeout_seconds=timeout_seconds)
        if not worked:
            await asyncio.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Durable OCR job worker")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_JOB_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.timeout_seconds < 1:
        parser.error("timeout must be positive")
    logging.basicConfig(level=logging.INFO)
    from ..model_artifacts import configured_model_directories
    configured_model_directories()  # Fail startup before claiming jobs if bundled models changed.
    asyncio.run(run_forever(timeout_seconds=args.timeout_seconds))


if __name__ == "__main__":
    main()
