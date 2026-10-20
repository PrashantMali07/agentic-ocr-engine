"""Live Temporal server durability suite (Phase 02 EXECUTION_PLAN step 6).

Opt in with ``RUN_LIVE_TEMPORAL_TESTS=true`` and a dev server reachable at
``INGESTION_TEMPORAL_ADDR`` (default localhost:7233). Start one with::

    temporal server start-dev --port 7233 --ui-port 8233 --db-filename /tmp/temporal-dev.db

Scenarios exercised against the real server:
1. Duplicate admission joins the running workflow (deterministic ID) —
   one logical run per admitted job; resubmit never forks a second run.
2. A SIGKILLed worker mid-execution never loses admitted work: the job
   completes exactly once from the client's perspective after restart,
   and page artifacts are deterministic (retries rewrite in place, never
   duplicate).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pymupdf
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ADDR = os.getenv("INGESTION_TEMPORAL_ADDR", "localhost:7233")
LIVE_ENABLED = os.getenv("RUN_LIVE_TEMPORAL_TESTS", "false").lower() == "true"
DIGITAL_LINE = "First digital page contains sufficient words for routing."
WORKER_READY_SECONDS = 8
REPO_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def _client_alive() -> bool:
    from temporalio.client import Client

    async def probe():
        return await Client.connect(SERVER_ADDR, namespace="default")

    try:
        asyncio.run(asyncio.wait_for(probe(), timeout=5))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not LIVE_ENABLED or not _client_alive(),
    reason=(
        "Set RUN_LIVE_TEMPORAL_TESTS=true and start a Temporal dev server with: "
        "`temporal server start-dev --port 7233 --ui-port 8233 --db-filename /tmp/temporal-dev.db`"
    ),
)


def make_mixed_pdf(path: Path):
    with pymupdf.open() as doc:
        page1 = doc.new_page()
        page1.insert_text((72, 72), DIGITAL_LINE)
        doc.new_page()  # scanned page: exercises the GPU queue, takes seconds on real models
        page3 = doc.new_page()
        page3.insert_text((72, 72), "Third digital page contains sufficient words too.")
        doc.save(path)


def make_digital_pdf(path: Path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), f"{DIGITAL_LINE} Concluding signed words present.")
        doc.save(path)


def _client() -> object:
    from temporalio.client import Client

    async def connect():
        return await Client.connect(SERVER_ADDR, namespace="default")

    return asyncio.run(connect())


@pytest.fixture(scope="module")
def live_client():
    return _client()


@pytest.fixture()
def isolated_root(tmp_path):
    """Per-test artifact tree; the worker subprocess inherits it via env."""
    output_root = tmp_path / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    env = {**REPO_ENV, "INGESTION_OUTPUT_DIR": str(output_root)}
    return output_root, env


def stage_input(output_root: Path, job_id: str, document_id: str, source: Path) -> None:
    target = ArtifactStore.from_output_root(output_root).artifact_path(
        job_id, "attachments", document_id, source.suffix.lower()
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())


@contextmanager
def lane_worker(env: dict):
    """Run the real worker daemon against the live server (isolated process)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "ingestion_pipeline.temporal.worker", "--server", SERVER_ADDR],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(WORKER_READY_SECONDS)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


async def _wait_for(handle, predicate, label: str, deadline: int = 90):
    for _ in range(deadline):
        status = await handle.query(DocumentIngestionWorkflow.get_status)  # noqa: F821
        if predicate(status):
            return status
        await asyncio.sleep(1)
    raise AssertionError(f"workflow never reached {label}")


# Imported late so the skip above happens before heavy modules load.
from ingestion_pipeline.storage.artifacts import ArtifactStore  # noqa: E402
from ingestion_pipeline.temporal import client as lane_client  # noqa: E402
from ingestion_pipeline.temporal.workflows import DocumentIngestionWorkflow  # noqa: E402


def test_duplicate_admission_joins_one_execution(live_client, isolated_root, tmp_path):
    output_root, env = isolated_root
    job_id = "a" * 32
    document_id = "b" * 32
    source = tmp_path / "dup.pdf"
    make_digital_pdf(source)
    stage_input(output_root, job_id, document_id, source)
    job_request = {
        "job_id": job_id,
        "documents": [{"document_id": document_id, "extension": ".pdf", "display_name": source.name}],
    }

    ids = []
    with lane_worker(env):
        pass
        async def scenario():
            for _ in range(2):
                ids.append(await lane_client.start_ingestion_workflow(job_request))
            handle = live_client.get_workflow_handle(ids[0])
            return await read_result(handle)

        result = asyncio.run(asyncio.wait_for(scenario(), timeout=600))

    assert ids[0] == ids[1] == f"doc-extract-{job_id}"
    assert result["status"] == "completed"
    assert result["pages"] == result["delivered_pages"] == 1
    # One logical run: the second admission joined; re-reading result is stable.
    again = asyncio.run(read_result(live_client.get_workflow_handle(ids[0])))
    assert again == result


def read_result(handle):
    async def read():
        return await handle.result()

    return read()


def test_worker_sigkill_never_loses_admitted_job(live_client, isolated_root, tmp_path):
    output_root, env = isolated_root
    job_id = "c" * 32
    document_id = "d" * 32
    source = tmp_path / "kill.pdf"
    make_mixed_pdf(source)
    stage_input(output_root, job_id, document_id, source)
    job_request = {
        "job_id": job_id,
        "documents": [{"document_id": document_id, "extension": ".pdf", "display_name": source.name}],
    }

    workflow_id = asyncio.run(lane_client.start_ingestion_workflow(job_request))
    handle = live_client.get_workflow_handle(workflow_id)

    async def wait_running():
        for _ in range(90):
            status = await handle.query(DocumentIngestionWorkflow.get_status)
            if status["status"] in {"running", "waiting_for_human_review"}:
                return status
            await asyncio.sleep(1)
        raise AssertionError("workflow never started running")

    with lane_worker(env) as worker:
        status = asyncio.run(asyncio.wait_for(wait_running(), timeout=120))
        # Mid-flight termination: worker dies while page activities are in flight.
        worker.send_signal(signal.SIGKILL)
    assert status["status"] == "running"

    with lane_worker(env):
        result = asyncio.run(asyncio.wait_for(read_result(handle), timeout=900))

    # Exactly-once from the client's perspective, after a hard kill + restart.
    assert result["pages"] == 3
    assert result["delivered_pages"] == 3
    assert result["status"] in {"completed", "partial", "needs_review"}
    assert not result["error"] or "activities exhausted" not in result["error"]

    reread = asyncio.run(asyncio.wait_for(read_result(handle), timeout=120))
    assert reread == result
    # Deterministic artifacts: every page outcome's artifact resolves.
    store = ArtifactStore.from_output_root(output_root)
    for outcome in result["page_outcomes"]:
        if outcome.get("text_artifact"):
            store.read_bounded(job_id, outcome["text_artifact"])
