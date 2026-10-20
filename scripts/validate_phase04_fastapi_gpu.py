"""Exercise scanned-PDF GPU OCR through the real FastAPI job route.

Starts ``uv run --no-sync ingest-api`` and two local worker processes with an
ephemeral API credential and SQLite/output directory. Submits three scanned
fixtures through HTTP, polls their results, and prints aggregate metrics only.
Nothing is deployed; all generated job data is removed on exit.
"""

import asyncio
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

from ingestion_pipeline.services.release_metrics import nearest_rank

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = tuple(ROOT / "test_asset" / f"RFQ_{number:02d}_scanned.pdf" for number in range(6, 9))
PORT = 8000  # ingest-api currently binds this port.
FINAL = {"completed", "partial", "waiting_for_review", "failed", "cancelled", "timed_out"}
SUCCESS = {"completed", "partial", "waiting_for_review"}


def gpu_name() -> str:
    probe = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError("NVIDIA driver is unavailable; GPU acceptance cannot run")
    return probe.stdout.splitlines()[0].strip()


def terminate(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


async def validate(token: str, rounds: int = 1) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    admitted: dict[str, tuple[str, float]] = {}
    processing: dict[str, float] = {}
    finished: dict[str, float] = {}
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=60, trust_env=False) as client:
        for _ in range(60):
            try:
                ready = await client.get("/ready")
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("FastAPI or worker did not become ready")

        async def submit(path: Path) -> str:
            with path.open("rb") as stream:
                response = await client.post(
                    "/extract/v1", headers=headers, data={"engine": "legacy"},
                    files={"files": (path.name, stream, "application/pdf")},
                )
            response.raise_for_status()
            job_id = response.json()["job_id"]
            admitted[job_id] = (path.name, time.monotonic())
            return job_id

        started = time.monotonic()
        for _ in range(rounds):
            batch_ids = await asyncio.gather(*(submit(path) for path in FIXTURES))
            deadline = time.monotonic() + 15 * 60
            while any(job_id not in finished for job_id in batch_ids):
                if time.monotonic() > deadline:
                    raise RuntimeError("GPU API jobs did not finish within 15 minutes")
                for job_id in batch_ids:
                    if job_id in finished:
                        continue
                    response = await client.get(f"/extract/v1/{job_id}", headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    status = result["status"]
                    if status == "processing":
                        processing.setdefault(job_id, time.monotonic())
                    if status in FINAL:
                        results[job_id] = result
                        finished[job_id] = time.monotonic()
                if any(job_id not in finished for job_id in batch_ids):
                    await asyncio.sleep(1)

        # The OCR child commits the terminal result before the supervising
        # worker persists its resource observation. Wait for every attempt.
        for _ in range(30):
            metrics_response = await client.get("/operations/metrics", headers=headers)
            metrics_response.raise_for_status()
            metrics = metrics_response.json()
            if sum(metrics["attempt_counts"].values()) >= len(admitted):
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Worker observations did not catch up with terminal jobs")
        final_ready = await client.get("/ready")

    statuses = Counter(result["status"] for result in results.values())
    nonempty = all(
        bool(result.get("result", {}).get("documents", {}).get(admitted[job_id][0], {}).get("content", "").strip())
        for job_id, result in results.items()
    )
    queue_waits = [processing[job_id] - admission_time for job_id, (_, admission_time) in admitted.items()
                   if job_id in processing]
    end_to_end = [finished[job_id] - admission_time for job_id, (_, admission_time) in admitted.items()]
    elapsed = time.monotonic() - started
    report = {
        "route": "POST /extract/v1 + GET /extract/v1/{job_id}",
        "gpu": gpu_name(),
        "fixture_count": len(FIXTURES),
        "rounds": rounds,
        "job_count": len(admitted),
        "worker_processes": 2,
        "statuses": dict(sorted(statuses.items())),
        "nonempty_document_results": nonempty,
        "wall_seconds": round(elapsed, 2),
        "throughput_jobs_per_minute": round(len(admitted) * 60 / elapsed, 3),
        "max_queue_wait_seconds": round(max(queue_waits), 2) if queue_waits else None,
        "max_end_to_end_seconds": round(max(end_to_end), 2),
        "p95_end_to_end_seconds": nearest_rank(end_to_end, 95),
        "p99_end_to_end_seconds": nearest_rank(end_to_end, 99),
        "failure_rate": round(sum(count for status, count in statuses.items()
                                  if status not in SUCCESS) / len(admitted), 4),
        "attempt_counts": metrics["attempt_counts"],
        "retry_attempts": metrics["retry_attempts"],
        "peak_rss_bytes": metrics["peak_rss_bytes"],
        "peak_gpu_bytes": metrics["peak_gpu_bytes"],
        "peak_artifact_bytes": metrics["peak_artifact_bytes"],
        "alerts_at_end": metrics["alerts"],
        "ready_at_end": final_ready.status_code == 200,
    }
    if (not nonempty or not set(statuses).issubset(SUCCESS)
            or sum(statuses.values()) != len(admitted)
            or metrics["retry_attempts"] != 0
            or not metrics["peak_gpu_bytes"] or final_ready.status_code != 200):
        raise RuntimeError("GPU FastAPI acceptance checks failed: " + json.dumps(report))
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1,
                        help="One to 100 sequential batches; API and workers remain running")
    parser.add_argument("--output", type=Path, help="Write aggregate JSON evidence to this file")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error("--rounds must be between 1 and 100")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as exc:
            raise SystemExit("Port 8000 is already in use; refusing to stop another service") from exc
    name = gpu_name()
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with tempfile.TemporaryDirectory(prefix="ingest-phase04-api-gpu-") as temporary:
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(ROOT / "src"),
            "PADDLE_USE_GPU": "true",
            "INGESTION_OUTPUT_DIR": temporary,
            "UV_CACHE_DIR": str(Path(temporary) / "uv-cache"),
            "INGESTION_API_KEY_SHA256_JSON": json.dumps({digest: {
                "tenant_id": "acceptance", "subject": "gpu-check", "roles": ["extract", "admin"],
            }}),
        })
        processes: list[subprocess.Popen] = []
        streams = []
        try:
            commands = [
                ["uv", "run", "--no-sync", "ingest-api"],
                [sys.executable, "-m", "ingestion_pipeline.services.worker"],
                [sys.executable, "-m", "ingestion_pipeline.services.worker"],
            ]
            for number, command in enumerate(commands):
                stream = (Path(temporary) / f"process-{number}.log").open("w", encoding="utf-8")
                streams.append(stream)
                processes.append(subprocess.Popen(
                    command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True,
                ))
            report = asyncio.run(validate(token, rounds=args.rounds))
            report["gpu"] = name
            if args.output:
                args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
        finally:
            terminate(processes)
            for stream in streams:
                stream.close()


if __name__ == "__main__":
    main()
