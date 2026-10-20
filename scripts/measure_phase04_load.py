"""Measure the active single-host OCR queue with simultaneous admissions.

Example:
  PYTHONPATH=src .venv/bin/python scripts/measure_phase04_load.py --device cpu

Only aggregate timing and resource metrics are printed. The fixture documents
and extracted text stay inside the temporary job directory, which is removed
at the end of the run. This is a bounded acceptance measurement, not a soak
test or an estimate of worst-case capacity.
"""

import argparse
import asyncio
import json
import multiprocessing
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_FIXTURES = tuple(f"RFQ_{number:02d}_scanned.pdf" for number in range(6, 11))
TERMINAL = {"completed", "partial", "waiting_for_review", "failed", "cancelled", "timed_out"}


def worker_process_main(db_path: str, job_ids: tuple[str, ...]) -> None:
    """Use a separate OS process and SQLite connection for each worker."""
    from ingestion_pipeline.job_store import JobStore
    from ingestion_pipeline.services.worker import run_once

    async def work() -> None:
        store = JobStore(Path(db_path))
        while True:
            if await run_once(store, timeout_seconds=20 * 60):
                continue
            states = [await store.get(job_id) for job_id in job_ids]
            if all(state is not None and state.status.value in TERMINAL for state in states):
                return
            await asyncio.sleep(0.2)

    asyncio.run(work())


async def measure(fixtures: tuple[str, ...], workers: int) -> dict:
    from ingestion_pipeline.job_store import JobStore
    from ingestion_pipeline.security.auth import Principal

    output_dir = Path(os.environ["INGESTION_OUTPUT_DIR"])
    store = JobStore(output_dir / "jobs.db")
    principal = Principal("acceptance", "local-load", frozenset({"extract"}))
    jobs = {}
    started = time.monotonic()

    async def admit(fixture: str) -> None:
        job_id = uuid.uuid4().hex
        document_id = uuid.uuid4().hex
        source = ROOT / "test_asset" / fixture
        attachment_dir = output_dir / "jobs" / job_id / "attachments"
        attachment_dir.mkdir(parents=True)
        stored = attachment_dir / f"{document_id}.pdf"
        shutil.copyfile(source, stored)
        await store.admit(job_id, principal, [{
            "document_id": document_id,
            "original_name": source.name,
            "size": stored.stat().st_size,
            "save_visualizations": False,
        }], stored.stat().st_size)
        jobs[job_id] = {"fixture": fixture, "admitted": time.monotonic()}

    # All clients submit without waiting for prior OCR work to finish.
    await asyncio.gather(*(admit(fixture) for fixture in fixtures))
    peak_running = peak_queued = 0

    async def observe() -> None:
        nonlocal peak_running, peak_queued
        while True:
            states = [(job_id, await store.get(job_id)) for job_id in jobs]
            running = sum(state is not None and state.status.value == "processing" for _, state in states)
            queued = sum(state is not None and state.status.value == "queued" for _, state in states)
            peak_running = max(peak_running, running)
            peak_queued = max(peak_queued, queued)
            now = time.monotonic()
            for job_id, state in states:
                if state is None:
                    continue
                if state.status.value == "processing" and "processing" not in jobs[job_id]:
                    jobs[job_id]["processing"] = now
                if state.status.value in TERMINAL and "finished" not in jobs[job_id]:
                    jobs[job_id]["finished"] = now
            if all(state is not None and state.status.value in TERMINAL for _, state in states):
                return
            await asyncio.sleep(0.2)

    context = multiprocessing.get_context("spawn")
    processes = [context.Process(
        target=worker_process_main, args=(str(output_dir / "jobs.db"), tuple(jobs)),
        name=f"phase04-worker-{number}",
    ) for number in range(workers)]
    for process in processes:
        process.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(observe(), *(asyncio.to_thread(process.join) for process in processes)),
            timeout=20 * 60 * len(fixtures) + 60,
        )
        if any(process.exitcode != 0 for process in processes):
            raise RuntimeError("A load-test worker exited unsuccessfully")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        await asyncio.gather(*(asyncio.to_thread(process.join, 5) for process in processes))
    elapsed = time.monotonic() - started
    results = {job_id: await store.get(job_id) for job_id in jobs}
    snapshot = await store.operational_snapshot()
    queue_waits = [entry["processing"] - entry["admitted"] for entry in jobs.values()
                   if "processing" in entry]
    latencies = [entry["finished"] - entry["admitted"] for entry in jobs.values()
                 if "finished" in entry]
    statuses = Counter(job.status.value for job in results.values() if job is not None)
    return {
        "fixture_count": len(fixtures),
        "worker_processes": workers,
        "statuses": dict(sorted(statuses.items())),
        "wall_seconds": round(elapsed, 2),
        "throughput_jobs_per_minute": round(len(results) * 60 / elapsed, 3),
        "peak_queued": peak_queued,
        "peak_processing": peak_running,
        "max_queue_wait_seconds": round(max(queue_waits), 2) if queue_waits else None,
        "max_end_to_end_seconds": round(max(latencies), 2) if latencies else None,
        "attempt_counts": snapshot["attempt_counts"],
        "max_attempt_duration_seconds": snapshot["max_duration_seconds"],
        "peak_rss_bytes": snapshot["peak_rss_bytes"],
        "peak_gpu_bytes": snapshot["peak_gpu_bytes"],
        "peak_artifact_bytes": snapshot["peak_artifact_bytes"],
        "alerts_at_end": snapshot["alerts"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "gpu"), required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--fixtures", nargs="+", choices=SCAN_FIXTURES,
                        default=list(SCAN_FIXTURES[:3]))
    args = parser.parse_args()
    if not 1 <= args.workers <= 4 or not 1 <= len(args.fixtures) <= 4:
        parser.error("Use one to four workers and one to four fixtures")
    if len(set(args.fixtures)) != len(args.fixtures):
        parser.error("Each fixture must be distinct")
    if args.device == "gpu":
        try:
            probe = subprocess.run(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            parser.error("GPU driver probe failed; cannot claim a GPU measurement")
        if probe.returncode != 0 or not probe.stdout.strip():
            parser.error("GPU driver is unavailable; cannot claim a GPU measurement")
        import paddle
        if not paddle.is_compiled_with_cuda() or not paddle.device.cuda.device_count():
            parser.error("GPU is unavailable; cannot claim a GPU measurement")
    os.environ["PADDLE_USE_GPU"] = "true" if args.device == "gpu" else "false"
    with tempfile.TemporaryDirectory(prefix="ingest-phase04-load-") as temporary:
        os.environ["INGESTION_OUTPUT_DIR"] = temporary
        report = asyncio.run(measure(tuple(args.fixtures), args.workers))
    report["requested_device"] = args.device
    print(json.dumps(report, indent=2))
    if sum(report["statuses"].values()) != len(args.fixtures) or any(
        status not in {"completed", "partial", "waiting_for_review"}
        for status in report["statuses"]
    ) or report["peak_processing"] > 1:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
