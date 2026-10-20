import asyncio
import sys

import pytest

from ingestion_pipeline.job_store import JobStore, QueueFull
from ingestion_pipeline.schema import JobStatus
from ingestion_pipeline.security.auth import Principal
from ingestion_pipeline.security.early_auth import EarlyDocumentAuth
from ingestion_pipeline.services import job_runner, worker
from ingestion_pipeline.services.worker import run_once

OWNER = Principal("tenant-a", "alice", frozenset({"extract"}))
OTHER = Principal("tenant-b", "bob", frozenset({"extract"}))


def manifest(name="invoice.pdf"):
    return [{"document_id": "a" * 32, "original_name": name, "size": 10, "save_visualizations": False}]


def test_atomic_admission_and_tenant_capacity(tmp_path):
    db_path = tmp_path / "jobs.db"

    async def run():
        first = JobStore(db_path)
        second = JobStore(db_path)
        await asyncio.gather(*[
            (first if i % 2 else second).admit(f"job-{i}", OWNER, manifest(), 10)
            for i in range(4)
        ])
        with pytest.raises(QueueFull):
            await first.admit("job-full", OWNER, manifest(), 10)
        await first.admit("other-tenant", OTHER, manifest(), 10)
        assert await second.get_for_principal("job-0", OTHER) is None
        assert (await second.get_for_principal("job-0", OWNER)).status == JobStatus.queued
    asyncio.run(run())


def test_submission_rate_limit_survives_queue_drain(tmp_path):
    async def run():
        store = JobStore(tmp_path / "jobs.db")
        for index in range(20):
            job_id = f"job-{index}"
            await store.admit(job_id, OWNER, manifest(), 10)
            assert await store.cancel(job_id, OWNER)
        with pytest.raises(QueueFull):
            await JobStore(tmp_path / "jobs.db").admit("job-21", OWNER, manifest(), 10)
    asyncio.run(run())


def test_claim_recovery_and_stale_worker_cannot_commit(tmp_path):
    async def run():
        db_path = tmp_path / "jobs.db"
        await JobStore(db_path).admit("job", OWNER, manifest(), 10)
        store = JobStore(db_path)  # A fresh worker process sees the persisted queue.
        first = await store.claim_next("worker-a", lease_seconds=-1)
        assert first and first[0] == "job" and first[2] == 1
        recovered = await store.claim_next("worker-b", lease_seconds=-1)
        assert recovered and recovered[2] == 2
        assert not await store.transition("job", "worker-a", JobStatus.completed, result={})
        assert await store.fail_exhausted_leases() == 1
        assert (await store.get("job")).status == JobStatus.failed
        assert await store.claim_next("worker-c") is None
    asyncio.run(run())


def test_cancel_is_owner_scoped_and_blocks_late_result(tmp_path):
    async def run():
        store = JobStore(tmp_path / "jobs.db")
        await store.admit("job", OWNER, manifest(), 10)
        assert await store.claim_next("worker")
        assert not await store.cancel("job", OTHER)
        assert await store.cancel("job", OWNER)
        assert not await store.renew_lease("job", "worker")
        assert not await store.transition("job", "worker", JobStatus.completed, result={})
        assert (await store.get("job")).status == JobStatus.cancelled
    asyncio.run(run())


def test_terminal_artifacts_are_cleaned_but_active_inputs_remain(tmp_path, monkeypatch):
    output = tmp_path / "output"
    monkeypatch.setattr(worker, "DEFAULT_OUTPUT_DIR", str(output))

    async def run():
        store = JobStore(tmp_path / "jobs.db")
        for job_id in ["cancelled", "active"]:
            folder = output / "jobs" / job_id / "attachments"
            folder.mkdir(parents=True)
            (folder / "sample.pdf").write_bytes(b"%PDF-1.7")
            await store.admit(job_id, OWNER, manifest(), 10)
        assert await store.cancel("cancelled", OWNER)
        await worker.cleanup_terminal_artifacts(store)
        assert not (output / "jobs" / "cancelled").exists()
        assert (output / "jobs" / "active").exists()
        assert "cancelled" not in await store.terminal_artifact_jobs()
    asyncio.run(run())


def test_child_runner_restores_display_names_and_commits(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    output = tmp_path / "output"
    job_dir = output / "jobs" / "job" / "attachments"
    job_dir.mkdir(parents=True)
    (job_dir / f"{'a' * 32}.pdf").write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(job_runner, "job_store", store)
    monkeypatch.setattr(job_runner, "DEFAULT_OUTPUT_DIR", str(output))

    def fake_extract(*, file_paths, email_id, **kwargs):
        assert file_paths == [job_dir / f"{'a' * 32}.pdf"]
        return {
            "email_id": email_id,
            "attachments": [{"file": file_paths[0].name, "format": "pdf", "pages": 1}],
            "blocks": [{"source_file": file_paths[0].name, "order": 0, "page": 1, "content": "extracted"}],
        }

    monkeypatch.setattr(job_runner, "process_email_attachments", fake_extract)

    async def admit_and_claim():
        await store.admit("job", OWNER, manifest(), 10)
        assert await store.claim_next("worker")
    asyncio.run(admit_and_claim())
    assert job_runner.run("job", "worker") == 0
    job = asyncio.run(store.get("job"))
    assert job.status == JobStatus.completed
    assert list(job.result.documents) == ["invoice.pdf"]
    assert job.progress.total == job.progress.processed == 1


def test_worker_kills_child_at_deadline(tmp_path):
    async def run():
        store = JobStore(tmp_path / "jobs.db")
        await store.admit("job", OWNER, manifest(), 10)
        assert await run_once(
            store, timeout_seconds=0.1,
            runner_command=[sys.executable, "-c", "import time; time.sleep(10)"],
        )
        assert (await store.get("job")).status == JobStatus.timed_out
    asyncio.run(run())


def test_unauthenticated_upload_is_rejected_without_reading_body():
    async def downstream(scope, receive, send):
        raise AssertionError("Unauthenticated request reached multipart parser")

    async def run():
        sent = []

        async def receive():
            raise AssertionError("Upload body was read")

        async def send(message):
            sent.append(message)

        await EarlyDocumentAuth(downstream)(
            {"type": "http", "method": "POST", "path": "/extract/v1", "headers": []},
            receive, send,
        )
        assert sent[0]["status"] == 401
    asyncio.run(run())
