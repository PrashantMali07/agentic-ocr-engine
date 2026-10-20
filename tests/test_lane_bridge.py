"""Phase 02 bridge tests: outbox reconciliation, result delivery, review notify.

All scenarios run against isolated temp ArtifactStore/JobStore instances
via the bridge's test seams (no Temporal server, no shared output root),
using the same legacy-shaped payload building the phase-03 assets tests
already exercise.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pymupdf
import pytest

from ingestion_pipeline import job_store as job_store_module
from ingestion_pipeline.job_store import JobStore
from ingestion_pipeline.storage.artifacts import ArtifactStore
from ingestion_pipeline.temporal import bridge

DIGITAL_LINE = "First digital page contains sufficient words for routing."


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    async def fast_init(store: JobStore):
        await store._ensure_init()

    store = JobStore(db_path=tmp_path / "jobs.db")
    artifact_store = ArtifactStore.from_output_root(tmp_path / "output")
    default_store = ArtifactStore.from_output_root(tmp_path / "default-store")
    monkeypatch.setattr(job_store_module, "job_store", store)
    bridge.set_job_store(store)
    bridge.STORE = artifact_store
    asyncio.run(fast_init(store))
    yield store, artifact_store
    bridge.set_job_store(job_store_module.job_store)
    bridge.STORE = default_store
    bridge.set_workflow_starter(None)
    bridge.set_workflow_status_querier(None)


def make_digital_pdf(path: Path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), f"{DIGITAL_LINE} Concluding signed words present.")
        doc.save(path)


def principal(roles=("extract",)):
    from ingestion_pipeline.security.auth import Principal

    return Principal(subject="owner-x", tenant_id="tenant-x", roles=set(roles))


def _principal(roles=("extract",)):
    return principal(roles)


# ---------------------------------------------------------------------------
# Outbox reconciliation
# ---------------------------------------------------------------------------

def test_outbox_row_survives_without_workflow_start(isolated_env):
    store, artifacts = isolated_env
    job_id = "a" * 32
    request = {"job_id": job_id, "documents": []}

    async def scenario():
        await store.admit(job_id, _principal(), [{"document_id": "1" * 32}], 1,
                          scheduler_kind=bridge.LANE_SCHEDULER, lane_request=request)
        pending = await store.pending_lane_submissions(bridge.LANE_SCHEDULER)
        assert pending and pending[0]["job_id"] == job_id
        submission = await store.submission_for_job(job_id)
        assert submission["workflow_id"] is None  # not yet started
        # A crash here is recoverable: the row stays pending and re-reconciles.

    asyncio.run(scenario())


def test_reconciler_starts_and_records_workflow(isolated_env):
    store, artifacts = isolated_env
    job_id = "b" * 32
    request = {"job_id": job_id, "documents": []}
    started = []

    async def fake_submitter(req):
        started.append(req)
        return f"doc-extract-{req['job_id']}"

    bridge.set_workflow_starter(fake_submitter)

    async def scenario():
        await store.admit(job_id, _principal(), [{"document_id": "1" * 32}], 1,
                          scheduler_kind=bridge.LANE_SCHEDULER, lane_request=request)
        count = await bridge.reconcile_pending_submissions()
        assert count == 1
        assert started == [request]
        submission = await store.submission_for_job(job_id)
        assert submission["workflow_id"] == f"doc-extract-{job_id}"
        # Second reconcile is a no-op for started rows.
        again = await bridge.reconcile_pending_submissions()
        assert again == 0
        assert len(started) == 1

    asyncio.run(scenario())


def test_reconciler_failure_keeps_row_pending(isolated_env):
    store, artifacts = isolated_env
    job_id = "c" * 32
    request = {"job_id": job_id, "documents": []}

    async def failing_submitter(req):
        raise RuntimeError("temporal server unreachable")

    bridge.set_workflow_starter(failing_submitter)

    async def scenario():
        await store.admit(job_id, _principal(), [{"document_id": "1" * 32}], 1,
                          scheduler_kind=bridge.LANE_SCHEDULER, lane_request=request)
        count = await bridge.reconcile_pending_submissions()
        assert count == 0  # row stays pending; retried on the next pass
        submission = await submit_retry(store, job_id)
        assert submission["workflow_id"] is None

    asyncio.run(scenario())


async def submit_retry(store, job_id):
    return await store.submission_for_job(job_id)


# ---------------------------------------------------------------------------
# Result delivery (guarded, idempotent)
# ---------------------------------------------------------------------------

def _stage_page_artifact(artifacts: ArtifactStore, job_id: str, document_id: str,
                         page_number: int, blocks: list) -> dict:
    artifact = artifacts.put_deterministic(
        job_id, "ocr", f"{document_id}:{page_number}:blocks", json.dumps(blocks).encode("utf-8"),
    )
    return {"artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"],
            "document_id": document_id, "page_number": page_number}


def test_deliver_result_completes_job_and_is_idempotent(isolated_env, tmp_path):
    store, artifacts = isolated_env
    job_id = "d" * 32
    document_id = "e" * 32
    source = tmp_path / "doc.pdf"
    make_digital_pdf(source)
    target = artifacts.artifact_path(job_id, "attachments", document_id, ".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())

    blocks = [{"block_label": "text", "content": DIGITAL_LINE, "order": 0,
               "page": 1, "confidence": 1.0, "source_file": "doc.pdf", "format": "pdf"}]
    ref = _stage_page_artifact(artifacts, job_id, document_id, 1, blocks)

    page_outcomes = [{
        "document_id": document_id, "display_name": "doc.pdf", "extension": "pdf",
        "page_number": 1, "total_pages": 1, "classification": "digital",
        "status": "completed", "error": None, "confidence": 1.0,
        "text_artifact": ref["artifact_id"], "text_artifact_sha256": ref["sha256"],
        "audit": {"is_valid": True, "requires_hitl": False, "issue_count": 0},
    }]
    final_payload = {
        "job_id": job_id, "status": "completed", "pages": 1, "delivered_pages": 1,
        "page_outcomes": page_outcomes, "artifact_refs": [ref],
        "avg_confidence": 1.0, "error": None,
        "engine_used": "digital_cpu", "human_reviewed": False,
    }

    async def scenario():

        # Job and lane outbox enter in one admission transaction.
        job_kwargs = {"job_id": job_id, "principal": _principal(), "manifest": [{"document_id": document_id}],
                      "reserved_bytes": 1024, "scheduler_kind": bridge.LANE_SCHEDULER,
                      "lane_request": {"job_id": job_id}}
        await store.admit(**job_kwargs)

        first = await bridge.deliver_workflow_result(final_payload)
        assert first is True
        job = await store.get(job_id)
        assert job.status.value == "completed"
        assert job.result is not None
        assert DIGITAL_LINE in job.result.documents["doc.pdf"].content

        # Idempotency: re-delivery of the same workflow outcome is a no-op.
        again = await bridge.deliver_workflow_result(final_payload)
        assert again is False
        unchanged = await store.get(job_id)
        assert unchanged.updated_at == job.updated_at

    asyncio.run(scenario())


def test_delivery_never_overwrites_legacy_jobs(isolated_env, tmp_path):
    store, artifacts = isolated_env
    job_id = "f" * 32

    async def scenario():
        # Legacy admitted job (no lane submission row).
        await store.admit(job_id=job_id, principal=_principal(), manifest=[{"document_id": "x"}],
                          reserved_bytes=1024)
        # (Deliberately not staged for the lane.)
        result = {"request_id": job_id, "documents": {}}
        delivered = await store.deliver_workflow_result(
            job_id, bridge.LANE_SCHEDULER, {**result, "status": "completed"},
        )
        assert delivered is False, "legacy job must not be claimed by the lane delivery"
        job = await store.get(job_id)
        assert job.status.value == "queued"

    asyncio.run(scenario())


def test_delivery_marks_review_and_blocks_result_overwrite(isolated_env, tmp_path, monkeypatch):
    store, artifacts = isolated_env
    job_id = "1" * 32
    document_id = "2" * 32
    source = tmp_path / "doc.pdf"
    make_digital_pdf(source)
    target = artifacts.artifact_path(job_id, "attachments", document_id, ".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())

    blocks = [{"block_label": "text", "content": DIGITAL_LINE, "order": 0,
               "page": 1, "confidence": 0.6, "source_file": "doc.pdf", "format": "pdf"}]
    ref = _stage_page_artifact(artifacts, job_id, document_id, 1, blocks)
    page_outcomes = [{
        "document_id": document_id, "display_name": "doc.pdf", "extension": "pdf",
        "page_number": 1, "total_pages": 1, "classification": "digital",
        "status": "completed", "error": None, "confidence": 0.6,
        "text_artifact": ref["artifact_id"], "text_artifact_sha256": ref["sha256"],
        "audit": {"is_valid": False, "requires_hitl": True, "issue_count": 2},
    }]
    final_payload = {
        "job_id": job_id, "status": "needs_review", "pages": 1, "delivered_pages": 1,
        "page_outcomes": page_outcomes, "artifact_refs": [ref],
        "avg_confidence": 0.6, "error": None,
        "engine_used": "digital_cpu", "human_reviewed": False,
    }

    async def scenario():
        await store.admit(job_id=job_id, principal=_principal(), manifest=[{"document_id": document_id}],
                          reserved_bytes=1024, scheduler_kind=bridge.LANE_SCHEDULER,
                          lane_request={"job_id": job_id})
        await store.record_lane_submission(job_id, f"doc-extract-{job_id}")

        delivered = await bridge.deliver_workflow_result(final_payload)
        assert delivered is True
        job = await store.get(job_id)
        assert job.status.value == "waiting_for_review"
        assert job.review_version == 0

        # Upstream review with the current version.
        from ingestion_pipeline.schema import requests as schema_requests

        review = schema_requests.ReviewRequest(
            document_name="doc.pdf", result_version=0,
            corrections=[schema_requests.ReviewCorrection(
                start=0, end=len(DIGITAL_LINE), expected=DIGITAL_LINE, replacement="Reviewed words.",
            )],
        )
        # API commit succeeds even when no Temporal signal has been sent yet.
        reviewed = await store.submit_review(job_id, _principal(("review",)), review)
        assert reviewed.status.value in {"completed", "partial"}
        signals = []

        async def send_signal(workflow_id, payload):
            signals.append((workflow_id, payload))

        from ingestion_pipeline.temporal import client as temporal_client
        monkeypatch.setattr(temporal_client, "send_human_review_signal", send_signal)
        assert await bridge.reconcile_pending_reviews() == 1
        assert signals[0][1]["result_version"] == 0
        assert await bridge.reconcile_pending_reviews() == 0

        # Late delivery from the workflow must not overwrite the reviewed state.
        again = await bridge.deliver_workflow_result(final_payload)
        assert again is False
        final = await store.get(job_id)
        assert final.status.value in {"completed", "partial"}
        assert final.reviewed_by == "owner-x"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Review notifier
# ---------------------------------------------------------------------------

def test_review_notifier_delivers_bounded_signal(isolated_env, tmp_path):
    store, artifacts = isolated_env
    job_id = "3" * 32
    document_id = "4" * 32
    source = tmp_path / "doc.pdf"
    make_digital_pdf(source)
    target = artifacts.artifact_path(job_id, "attachments", document_id, ".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())

    blocks = [{"block_label": "text", "content": DIGITAL_LINE, "order": 0,
               "page": 1, "confidence": 0.5, "source_file": "doc.pdf", "format": "pdf"}]
    ref = _stage_page_artifact(artifacts, job_id, document_id, 1, blocks)
    page_outcomes = [{
        "document_id": document_id, "display_name": "doc.pdf", "extension": "pdf",
        "page_number": 1, "total_pages": 1, "classification": "digital",
        "status": "completed", "error": None, "confidence": 0.5,
        "text_artifact": ref["artifact_id"], "text_artifact_sha256": ref["sha256"],
        "audit": {"is_valid": False, "requires_hitl": True, "issue_count": 1},
    }]
    final_payload = {
        "job_id": job_id, "status": "needs_review", "pages": 1, "delivered_pages": 1,
        "page_outcomes": page_outcomes, "artifact_refs": [ref],
        "avg_confidence": 0.5, "error": None,
        "engine_used": "digital_cpu", "human_reviewed": False,
    }
    signals = []

    async def scenario():
        from ingestion_pipeline.schema import requests as schema_requests

        await store.admit(job_id=job_id, principal=_principal(), manifest=[{"document_id": document_id}],
                          reserved_bytes=1024, scheduler_kind=bridge.LANE_SCHEDULER,
                          lane_request={"job_id": job_id})
        # Reconciliation phase completed in production: the workflow ID is recorded.
        await store.record_lane_submission(job_id, f"doc-extract-{job_id}")
        await bridge.deliver_workflow_result(final_payload)

        async def fake_signal(workflow_id, payload):
            signals.append((workflow_id, payload))

        import ingestion_pipeline.temporal.client as client_module

        monkeypatch_target = client_module
        original = monkeypatch_target.send_human_review_signal
        monkeypatch_target.send_human_review_signal = fake_signal
        try:
            await bridge._deliver_lane_review_signal(
                job_id,
                schema_requests.ReviewRequest(
                    document_name="doc.pdf", result_version=0,
                    corrections=[schema_requests.ReviewCorrection(
                        start=0, end=5, expected=DIGITAL_LINE[:5],
                        replacement="Fixed",
                    )],
                ),
            )
        finally:
            monkeypatch_target.send_human_review_signal = original

        workflow_id, payload = signals[0]
        assert workflow_id == f"doc-extract-{job_id}"
        # Bounded metadata only — no document text flies to the workflow.
        assert "result_version" in payload
        assert all(set(c) <= {"start", "end", "replacement_length", "has_expected"} for c in payload["corrections"])
        assert DIGITAL_LINE not in json.dumps(payload)

    asyncio.run(scenario())


def test_notifier_missing_workflow_id_logs_and_continues(isolated_env):
    store, artifacts = isolated_env
    job_id = "5" * 32

    async def scenario():
        from ingestion_pipeline.schema import requests as schema_requests

        # No workflow ID yet: notification must be a safe no-op.
        await bridge._deliver_lane_review_signal(
            job_id,
            schema_requests.ReviewRequest(document_name="doc.pdf", result_version=0, corrections=[]),
        )

    asyncio.run(scenario())  # does not raise


def test_cancelled_lane_is_not_started_and_running_lane_is_cancelled(isolated_env, monkeypatch):
    store, _ = isolated_env
    cancelled = []

    async def fake_cancel(workflow_id):
        cancelled.append(workflow_id)

    from ingestion_pipeline.temporal import client as temporal_client
    monkeypatch.setattr(temporal_client, "cancel_ingestion_workflow", fake_cancel)

    async def scenario():
        before_start = "6" * 32
        await store.admit(before_start, _principal(), [{"document_id": "a" * 32}], 1,
                          scheduler_kind=bridge.LANE_SCHEDULER, lane_request={"job_id": before_start})
        await store.cancel(before_start, _principal())
        assert await store.pending_lane_submissions(bridge.LANE_SCHEDULER) == []

        running = "7" * 32
        await store.admit(running, _principal(), [{"document_id": "b" * 32}], 1,
                          scheduler_kind=bridge.LANE_SCHEDULER, lane_request={"job_id": running})
        await store.record_lane_submission(running, f"doc-extract-{running}")
        await store.cancel(running, _principal())
        assert await bridge.reconcile_pending_cancellations() == 1
        assert cancelled == [f"doc-extract-{running}"]
        assert await bridge.reconcile_pending_cancellations() == 0
        assert not await store.deliver_workflow_result(
            running, bridge.LANE_SCHEDULER, {"request_id": running, "documents": {}},
        )

    asyncio.run(scenario())


def test_incomplete_workflow_pages_cannot_complete_api_job(isolated_env):
    store, _ = isolated_env
    job_id = "8" * 32
    document_id = "9" * 32

    async def scenario():
        await store.admit(job_id, _principal(), [{"document_id": document_id}], 1,
                          scheduler_kind=bridge.LANE_SCHEDULER, lane_request={"job_id": job_id})
        reported_complete = {
            "job_id": job_id, "status": "completed", "artifact_refs": [],
            "page_outcomes": [{
                "document_id": document_id, "display_name": "incomplete.pdf", "extension": "pdf",
                "page_number": 1, "total_pages": 2, "classification": "digital",
                "status": "completed", "audit": {"is_valid": True, "requires_hitl": False},
            }],
        }
        assert await bridge.deliver_workflow_result(reported_complete)
        job = await store.get(job_id)
        assert job.status.value == "failed"
        assert job.result.documents == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(("error", "expected"), [
    ("review_timeout", "timed_out"),
    ("review_confirmation_failed", "failed"),
])
def test_waiting_review_closes_when_workflow_fails(isolated_env, error, expected):
    store, _ = isolated_env
    job_id = "f" * 32

    async def fake_status(_job_id):
        return {"status": "failed", "error": error}

    bridge.set_workflow_status_querier(fake_status)

    async def scenario():
        await store.admit(job_id, _principal(), [{"document_id": "a" * 32}], 1,
                          scheduler_kind=bridge.LANE_SCHEDULER, lane_request={"job_id": job_id})
        await store.record_lane_submission(job_id, f"doc-extract-{job_id}")
        assert await store.deliver_workflow_result(
            job_id, bridge.LANE_SCHEDULER,
            {"request_id": job_id, "documents": {}}, needs_review=True,
        )
        assert await bridge.reconcile_failed_review_waits() == 1
        assert (await store.get(job_id)).status.value == expected

    asyncio.run(scenario())
