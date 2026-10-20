"""Run one admitted OCR job in a killable child process."""

import asyncio
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

from ..api import transform_to_document_map
from ..config import DEFAULT_OUTPUT_DIR
from ..extraction.manager import process_email_attachments
from ..job_store import job_store
from ..processing.preflight import PreflightError
from ..schema import ExtractResponse, JobStatus
from ..storage.uploads import validate_name

log = logging.getLogger("ingest.runner")
MAX_RESULT_BYTES = 8 * 1024 * 1024


def _input_paths(job_id: str, manifest: list[dict]) -> tuple[list[Path], dict[str, str]]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
        raise ValueError("Invalid job identifier")
    if not manifest or len(manifest) > 8:
        raise ValueError("Invalid job manifest")
    root = (Path(DEFAULT_OUTPUT_DIR) / "jobs" / job_id / "attachments").resolve()
    paths = []
    names = {}
    for item in manifest:
        document_id = item.get("document_id")
        original_name, ext = validate_name(item.get("original_name"))
        if not isinstance(document_id, str) or len(document_id) != 32 or any(c not in "0123456789abcdef" for c in document_id):
            raise ValueError("Invalid document identifier")
        path = root / f"{document_id}{ext}"
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise ValueError("Job input is missing or unsafe")
        paths.append(path)
        names[path.name] = original_name
    return paths, names


def _watch_parent(expected_parent: int) -> None:
    while True:
        if os.getppid() != expected_parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.25)


def run(job_id: str, worker_id: str, expected_parent: int | None = None) -> int:
    if expected_parent is not None:
        if os.getppid() != expected_parent:
            return 2
        threading.Thread(target=_watch_parent, args=(expected_parent,), daemon=True).start()
    manifest = asyncio.run(job_store.manifest_for_worker(job_id, worker_id))
    if manifest is None:
        return 0  # Already cancelled or completed.
    try:
        paths, names = _input_paths(job_id, manifest)
        output_dir = Path(DEFAULT_OUTPUT_DIR) / "jobs" / job_id / "output"
        record = process_email_attachments(
            file_paths=paths,
            output_dir=str(output_dir),
            email_id=job_id,
            request_id=job_id,
            save_visualizations=bool(manifest[0].get("save_visualizations", False)),
        )
        result = transform_to_document_map(record)
        result["request_id"] = job_id
        result["documents"] = {names.get(key, key): value for key, value in result["documents"].items()}
        validated = ExtractResponse(**result)
        if len(validated.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
            raise PreflightError("output_limit", "Extraction result exceeds the supported limit")
        needs_review = any(
            doc.correction_suggestions or
            (doc.audit and (doc.audit.get("requires_hitl") is True or doc.audit.get("needs_review") is True))
            for doc in validated.documents.values()
        )
        final_status = (JobStatus.waiting_for_review if needs_review else
                        JobStatus.partial if any(doc.status == "partial" for doc in validated.documents.values())
                        else JobStatus.completed)
        committed = asyncio.run(job_store.transition(
            job_id, worker_id, final_status, result=validated,
        ))
        return 0 if committed else 2
    except PreflightError as exc:
        log.warning("Job %s rejected: %s", job_id, exc.code)
        asyncio.run(job_store.transition(
            job_id, worker_id, JobStatus.failed, error=f"{exc.code}: {exc}",
        ))
        return 0
    except Exception as exc:
        log.error("Job %s failed in child process (%s)", job_id, type(exc).__name__)
        return 1


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 4:
        raise SystemExit("Usage: python -m ingestion_pipeline.services.job_runner JOB_ID WORKER_ID PARENT_PID")
    raise SystemExit(run(sys.argv[1], sys.argv[2], int(sys.argv[3])))


if __name__ == "__main__":
    main()
