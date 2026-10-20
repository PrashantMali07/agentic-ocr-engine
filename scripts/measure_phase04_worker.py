"""Run one local fixture through the durable worker and print non-document metrics.

Examples:
  .venv/bin/python scripts/measure_phase04_worker.py --device cpu
  .venv/bin/python scripts/measure_phase04_worker.py --device gpu
"""

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ("RFQ_06_scanned.pdf", "RFQ_01_digital.pdf")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "gpu"), required=True)
    parser.add_argument("--fixture", choices=FIXTURES, default=FIXTURES[0])
    args = parser.parse_args()
    os.environ["PADDLE_USE_GPU"] = "true" if args.device == "gpu" else "false"
    source = ROOT / "test_asset" / args.fixture

    with tempfile.TemporaryDirectory(prefix="ingest-phase04-") as temporary:
        os.environ["INGESTION_OUTPUT_DIR"] = temporary
        from ingestion_pipeline.job_store import JobStore
        from ingestion_pipeline.security.auth import Principal
        from ingestion_pipeline.services.worker import run_once

        async def run() -> dict:
            store = JobStore(Path(temporary) / "jobs.db")
            job_id = uuid.uuid4().hex
            document_id = uuid.uuid4().hex
            attachment_dir = Path(temporary) / "jobs" / job_id / "attachments"
            attachment_dir.mkdir(parents=True)
            stored = attachment_dir / f"{document_id}.pdf"
            shutil.copyfile(source, stored)
            owner = Principal("fixture", "local-benchmark", frozenset({"extract"}))
            await store.admit(job_id, owner, [{
                "document_id": document_id, "original_name": source.name,
                "size": stored.stat().st_size, "save_visualizations": False,
            }], stored.stat().st_size)
            await run_once(store, timeout_seconds=20 * 60)
            job = await store.get(job_id)
            if job is None:
                raise RuntimeError("Worker did not leave a job record")
            snapshot = await store.operational_snapshot()
            return {
                "fixture": source.name, "requested_device": args.device,
                "status": job.status.value, "document_status": (
                    job.result.documents[source.name].status if job.result else None
                ),
                "attempt_counts": snapshot["attempt_counts"],
                "max_duration_seconds": snapshot["max_duration_seconds"],
                "peak_rss_bytes": snapshot["peak_rss_bytes"],
                "peak_gpu_bytes": snapshot["peak_gpu_bytes"],
                "peak_artifact_bytes": snapshot["peak_artifact_bytes"],
            }

        print(json.dumps(asyncio.run(run()), indent=2))


if __name__ == "__main__":
    main()
