"""Run a scheduled, multi-tenant GPU workload through the real FastAPI route.

The manifest expands bounded series of local test_asset documents into jobs.
Aggregate JSON excludes document text; a separate private per-job report binds
each result to its fixture hash, tenant, timing, and final status. No image is
built and no service outside localhost is contacted.
"""

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

from ingestion_pipeline.services.release_metrics import nearest_rank

if __package__:
    from .private_evidence import sha256_file, write_private_json
    from .validate_phase04_fastapi_gpu import PORT, SUCCESS, gpu_name, terminate
else:
    from private_evidence import sha256_file, write_private_json
    from validate_phase04_fastapi_gpu import PORT, SUCCESS, gpu_name, terminate


ROOT = Path(__file__).resolve().parent.parent
ASSETS = (ROOT / "test_asset").resolve()
FORMATS = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".html", ".csv", ".xlsx"}
MAX_JOBS = 500
MAX_OFFSET = 24 * 60 * 60
TERMINAL = SUCCESS | {"failed", "cancelled", "timed_out"}


def expand_manifest(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    series = payload.get("series") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(series, list) or not series:
        raise ValueError("Workload manifest needs a nonempty schema-version-1 series")
    cases = []
    seen = set()
    for group in series:
        if not isinstance(group, dict):
            raise ValueError("Each workload series must be an object")
        name, fixture, tenant, category = (group.get(key) for key in
                                           ("id", "fixture", "tenant", "category"))
        count = group.get("count")
        start, every = group.get("start_seconds"), group.get("every_seconds")
        expect = group.get("expect", "accepted")
        if (not all(isinstance(value, str) and value.strip() for value in
                    (name, fixture, tenant, category))
                or name in seen or not isinstance(count, int) or isinstance(count, bool)
                or not 1 <= count <= MAX_JOBS or expect not in {"accepted", "rejected"}
                or isinstance(start, bool) or not isinstance(start, (int, float))
                or isinstance(every, bool) or not isinstance(every, (int, float))
                or start < 0 or every < 0 or start + every * (count - 1) > MAX_OFFSET):
            raise ValueError("Workload series has invalid identity, count, expectation, or timing")
        seen.add(name)
        candidate = ASSETS / fixture
        source = candidate.resolve()
        if (source.suffix.lower() not in FORMATS or not source.is_relative_to(ASSETS)
                or not source.is_file() or candidate.is_symlink()):
            raise ValueError("Workload fixture must be a supported file inside test_asset")
        size = source.stat().st_size
        if size <= 0 or size > 60 * 1024 * 1024:
            raise ValueError("Workload fixture is empty or exceeds the local file budget")
        digest = sha256_file(source)
        for index in range(count):
            cases.append({"case_id": f"{name}-{index + 1:04d}", "source": source,
                          "fixture": fixture, "source_sha256": digest, "size_bytes": size,
                          "format": source.suffix.lower().lstrip("."), "tenant": tenant,
                          "category": category, "scheduled_at_seconds": round(start + every * index, 3),
                          "expect": expect})
            if len(cases) > MAX_JOBS:
                raise ValueError("Workload exceeds the 500-job safety bound")
    if len({case["tenant"] for case in cases}) > 8:
        raise ValueError("Workload exceeds eight test tenants")
    return sorted(cases, key=lambda case: (case["scheduled_at_seconds"], case["case_id"]))


async def run_workload(cases: list[dict], tokens: dict[str, str], *,
                       job_timeout: int = 1800, poll_interval: float = 2.0) -> tuple[dict, dict]:
    if not 1 <= job_timeout <= 7200 or not 0.2 <= poll_interval <= 30:
        raise ValueError("Job timeout or polling interval is outside safe bounds")
    records = []
    started = time.monotonic()
    peak_queued = peak_processing = 0
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}",
                                 timeout=httpx.Timeout(60, connect=5), trust_env=False) as client:
        for _ in range(60):
            try:
                ready = await client.get("/ready")
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("FastAPI and worker did not become ready")

        async def one(case: dict) -> None:
            await asyncio.sleep(max(0, started + case["scheduled_at_seconds"] - time.monotonic()))
            record = {key: value for key, value in case.items() if key != "source"}
            record["submitted_at_seconds"] = round(time.monotonic() - started, 3)
            headers = {"Authorization": f"Bearer {tokens[case['tenant']]}"}
            content_type = mimetypes.guess_type(case["fixture"])[0] or "application/octet-stream"
            with case["source"].open("rb") as stream:
                response = await client.post("/extract/v1", headers=headers,
                    data={"engine": "legacy"},
                    files={"files": (case["source"].name, stream, content_type)})
            record["admission_http_status"] = response.status_code
            if response.status_code == 429:
                record["status"] = "rejected"
                record["finished_at_seconds"] = round(time.monotonic() - started, 3)
                records.append(record)
                return
            response.raise_for_status()
            if response.status_code != 202:
                raise RuntimeError("Unexpected job admission response")
            job_id = response.json()["job_id"]
            record["job_id"] = job_id
            record["admitted_at_seconds"] = round(time.monotonic() - started, 3)
            deadline = time.monotonic() + job_timeout
            while time.monotonic() < deadline:
                polled = await client.get(f"/extract/v1/{job_id}", headers=headers)
                polled.raise_for_status()
                payload = polled.json()
                status = payload["status"]
                if status == "processing" and "processing_at_seconds" not in record:
                    record["processing_at_seconds"] = round(time.monotonic() - started, 3)
                if status in TERMINAL:
                    record["status"] = status
                    record["finished_at_seconds"] = round(time.monotonic() - started, 3)
                    document = payload.get("result") or {}
                    documents = document.get("documents") or {}
                    record["nonempty_result"] = bool(
                        documents.get(case["source"].name, {}).get("content", "").strip())
                    record["end_to_end_seconds"] = round(
                        record["finished_at_seconds"] - record["admitted_at_seconds"], 3)
                    records.append(record)
                    return
                await asyncio.sleep(poll_interval)
            raise RuntimeError("GPU job exceeded its bounded completion deadline")

        stop_sampling = asyncio.Event()

        async def sample_queue() -> None:
            nonlocal peak_queued, peak_processing
            headers = {"Authorization": f"Bearer {tokens[cases[0]['tenant']]}"}
            while not stop_sampling.is_set():
                response = await client.get("/operations/metrics", headers=headers)
                response.raise_for_status()
                counts = response.json().get("job_counts", {})
                peak_queued = max(peak_queued, counts.get("queued", 0))
                peak_processing = max(peak_processing, counts.get("processing", 0))
                try:
                    await asyncio.wait_for(stop_sampling.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass

        sampler = asyncio.create_task(sample_queue())
        try:
            await asyncio.gather(*(one(case) for case in cases))
        finally:
            stop_sampling.set()
            await sampler
        accepted = [record for record in records if record["admission_http_status"] == 202]
        for _ in range(60):
            response = await client.get("/operations/metrics",
                headers={"Authorization": f"Bearer {tokens[cases[0]['tenant']]}"})
            response.raise_for_status()
            metrics = response.json()
            if sum(metrics["attempt_counts"].values()) >= len(accepted):
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Worker observations did not catch up with terminal jobs")
        final_ready = await client.get("/ready")

    records.sort(key=lambda record: record["case_id"])
    elapsed = time.monotonic() - started
    statuses = Counter(record["status"] for record in accepted)
    classes = Counter(record["category"] for record in accepted)
    intervals = sorted({round(cases[index]["scheduled_at_seconds"] -
                              cases[index - 1]["scheduled_at_seconds"], 3)
                        for index in range(1, len(cases))})
    latencies = [record["end_to_end_seconds"] for record in accepted]
    report = {
        "route": "POST /extract/v1 + GET /extract/v1/{job_id}",
        "gpu": gpu_name(), "profile": "representative", "job_count": len(accepted),
        "scheduled_count": len(cases), "rejected_count": len(records) - len(accepted),
        "fixture_count": len({record["source_sha256"] for record in accepted}),
        "document_classes": dict(sorted(classes.items())),
        "tenant_count": len({record["tenant"] for record in accepted}),
        "distinct_input_sizes": len({record["size_bytes"] for record in accepted}),
        "arrival_span_seconds": max(case["scheduled_at_seconds"] for case in cases)
                                - min(case["scheduled_at_seconds"] for case in cases),
        "arrival_intervals_seconds": intervals,
        "peak_queued": peak_queued, "peak_processing": peak_processing,
        "statuses": dict(sorted(statuses.items())),
        "nonempty_document_results": all(record["nonempty_result"] for record in accepted),
        "wall_seconds": round(elapsed, 2),
        "throughput_jobs_per_minute": round(len(accepted) * 60 / elapsed, 3),
        "max_queue_wait_seconds": round(max((record["processing_at_seconds"] -
              record["admitted_at_seconds"] for record in accepted
              if "processing_at_seconds" in record), default=0), 2),
        "p95_end_to_end_seconds": nearest_rank(latencies, 95),
        "p99_end_to_end_seconds": nearest_rank(latencies, 99),
        "failure_rate": round(sum(count for status, count in statuses.items()
              if status not in SUCCESS) / len(accepted), 4) if accepted else 1.0,
        "attempt_counts": metrics["attempt_counts"], "retry_attempts": metrics["retry_attempts"],
        "peak_rss_bytes": metrics["peak_rss_bytes"],
        "peak_gpu_bytes": metrics["peak_gpu_bytes"],
        "peak_artifact_bytes": metrics["peak_artifact_bytes"],
        "alerts_at_end": metrics["alerts"], "ready_at_end": final_ready.status_code == 200,
    }
    return report, {"schema_version": 1, "jobs": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs-output", required=True, type=Path)
    parser.add_argument("--job-timeout", type=int, default=1800)
    args = parser.parse_args()
    try:
        cases = expand_manifest(args.manifest)
        if args.output.resolve() == args.jobs_output.resolve():
            raise ValueError("Aggregate and per-job output paths must differ")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", PORT))
        gpu = gpu_name()
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        parser.error(f"Workload preflight failed ({type(exc).__name__}: {exc})")

    tokens = {tenant: secrets.token_urlsafe(32) for tenant in {case["tenant"] for case in cases}}
    credentials = {hashlib.sha256(token.encode()).hexdigest(): {
        "tenant_id": tenant, "subject": f"load-{tenant}", "roles": ["extract", "admin"]}
        for tenant, token in tokens.items()}
    with tempfile.TemporaryDirectory(prefix="ingest-representative-gpu-") as temporary:
        env = os.environ.copy()
        env.update({"PYTHONPATH": str(ROOT / "src"), "PADDLE_USE_GPU": "true",
                    "INGESTION_OUTPUT_DIR": temporary,
                    "UV_CACHE_DIR": str(Path(temporary) / "uv-cache"),
                    "INGESTION_API_KEY_SHA256_JSON": json.dumps(credentials)})
        processes = []
        streams = []
        try:
            commands = [["uv", "run", "--no-sync", "ingest-api"],
                        [sys.executable, "-m", "ingestion_pipeline.services.worker"],
                        [sys.executable, "-m", "ingestion_pipeline.services.worker"]]
            for index, command in enumerate(commands):
                stream = (Path(temporary) / f"process-{index}.log").open("w", encoding="utf-8")
                streams.append(stream)
                processes.append(subprocess.Popen(command, cwd=ROOT, env=env,
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True))
            report, job_evidence = asyncio.run(run_workload(cases, tokens, job_timeout=args.job_timeout))
            report["gpu"] = gpu
            report["workload_manifest_sha256"] = sha256_file(args.manifest)
            report["per_job_evidence_sha256"] = write_private_json(args.jobs_output, job_evidence)
            write_private_json(args.output, report)
            print(json.dumps(report, indent=2))
            if (report["rejected_count"] != sum(case["expect"] == "rejected" for case in cases)
                    or any((record["status"] == "rejected") != (record["expect"] == "rejected")
                           for record in job_evidence["jobs"])
                    or not set(report["statuses"]).issubset(SUCCESS)
                    or not report["nonempty_document_results"] or not report["ready_at_end"]):
                raise SystemExit(1)
        finally:
            terminate(processes)
            for stream in streams:
                stream.close()


if __name__ == "__main__":
    main()
