"""Phase 02 integration tests: Temporal test lane end-to-end.

Real workflow environment (time-skipping), real workers on both queues,
and the Phase 01 shared service underneath. GPU OCR is exercised through
the same monkeypatchable seams the existing regression suite uses, so
these tests never require the Paddle model stack. Async orchestration
follows the repository convention (asyncio.run inside sync tests).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pymupdf
import pytest
from temporalio import activity as temporal_activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ingestion_pipeline.config import DEFAULT_OUTPUT_DIR
from ingestion_pipeline.storage.artifacts import ArtifactStore
from ingestion_pipeline.temporal.activities import (
    CPU_QUEUE,
    GPU_QUEUE,
    audit_page_activity,
    confirm_review_activity,
    digital_page_activity,
    dispatch_webhook_activity,
    image_page_activity,
    native_document_activity,
    plan_document_activity,
    route_page_activity,
    scanned_page_activity,
)
from ingestion_pipeline.temporal.workflows import DocumentIngestionWorkflow

DIGITAL_LINE = "First digital page contains sufficient words for routing."


def _make_digital_pdf(path: Path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), f"{DIGITAL_LINE} Extra concluding words present.")
        doc.save(path)


def _make_mixed_pdf(path: Path):
    with pymupdf.open() as doc:
        page1 = doc.new_page()
        page1.insert_text((72, 72), DIGITAL_LINE)
        doc.new_page()  # scanned page: no text layer
        page3 = doc.new_page()
        page3.insert_text((72, 72), "Third digital page contains sufficient words too.")
        doc.save(path)


def stage(store: ArtifactStore, job_id: str, document_id: str, source: Path):
    target = store.artifact_path(job_id, "attachments", document_id, source.suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target


@pytest.fixture()
def store():
    return ArtifactStore.from_output_root(Path(DEFAULT_OUTPUT_DIR))


def test_digital_pdf_workflow_completes(store, monkeypatch, tmp_path):
    job_id = "1" * 32
    document_id = "a" * 32
    source = tmp_path / "rfq01.pdf"
    _make_digital_pdf(source)
    stage(store, job_id, document_id, source)
    job_request = {
        "job_id": job_id,
        "documents": [{"document_id": document_id, "extension": ".pdf",
                       "display_name": "RFQ_01_digital.pdf"}],
    }

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue=CPU_QUEUE, workflows=[DocumentIngestionWorkflow],
                activities=[plan_document_activity, route_page_activity, audit_page_activity, confirm_review_activity, digital_page_activity,
                            native_document_activity, dispatch_webhook_activity],
                max_concurrent_activities=16,
            ):
                handle = await env.client.start_workflow(
                    DocumentIngestionWorkflow.run,
                    args=[job_request],
                    id=f"doc-extract-{job_id}",
                    task_queue=CPU_QUEUE,
                )
                return await handle.result()

    result = asyncio.run(scenario())

    assert result["status"] == "completed"
    assert result["pages"] == result["delivered_pages"] == 1
    assert result["page_outcomes"][0]["status"] == "completed"
    text_artifact = result["page_outcomes"][0]["text_artifact"]
    stored = store.read_bounded(job_id, text_artifact)
    assert "sufficient words" in stored.decode("utf-8")
    # Workflow history must not carry raw document text: the result payload
    # contains statuses, artifact IDs and confidence only.
    payload_text = repr(result)
    assert "sufficient words" not in payload_text


def test_mixed_pdf_page_accounting(store, monkeypatch, tmp_path):
    job_id = "2" * 32
    document_id = "b" * 32
    source = tmp_path / "mixed.pdf"
    _make_mixed_pdf(source)
    stage(store, job_id, document_id, source)

    import numpy as np

    from ingestion_pipeline.extraction import manager as manager_module

    monkeypatch.setattr(manager_module, "get_ocr_pipeline", lambda: object())
    monkeypatch.setattr(manager_module, "prepare_image_for_ocr",
                        lambda path: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(manager_module, "ocr_predict", lambda pipeline, image: [object()])
    monkeypatch.setattr(manager_module, "extract_tesseract_lines", lambda image: [])
    monkeypatch.setattr(manager_module, "pp_structure_to_blocks", lambda result, source_name, page: ([{
        "block_label": "text", "content": "Second scanned page text", "order": 0,
        "page": page, "confidence": 0.99, "source_file": source_name, "format": "pdf",
    }]))

    job_request = {
        "job_id": job_id,
        "documents": [{"document_id": document_id, "extension": ".pdf", "display_name": "mixed.pdf"}],
    }

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue=CPU_QUEUE, workflows=[DocumentIngestionWorkflow],
                activities=[plan_document_activity, route_page_activity, audit_page_activity, confirm_review_activity, digital_page_activity,
                            native_document_activity, dispatch_webhook_activity],
            ):
                async with Worker(
                    env.client, task_queue=GPU_QUEUE,
                    activities=[scanned_page_activity, image_page_activity],
                    max_concurrent_activities=1,
                ):
                    handle = await env.client.start_workflow(
                        DocumentIngestionWorkflow.run, args=[job_request],
                        id=f"doc-extract-{job_id}", task_queue=CPU_QUEUE,
                    )
                    return await handle.result()

    result = asyncio.run(scenario())

    assert result["status"] == "completed"
    statuses = [o["status"] for o in result["page_outcomes"]]
    assert statuses == ["completed", "completed", "completed"]
    assert result["pages"] == 3
    assert result["artifact_refs"], "scanned+digital pages should write text artifacts"


def test_missing_planned_page_cannot_be_success(store, tmp_path):
    job_id = "3" * 32
    document_id = "c" * 32
    real_plan = plan_document_activity
    resolved = Path("unused")

    @temporal_activity.defn(name="plan_document_activity")
    async def planning_with_vanished_input(request):
        nonlocal resolved
        plan = await real_plan(request)
        resolved = store.artifact_path(job_id, "attachments", document_id, ".pdf")
        resolved.unlink()
        return plan

    source = tmp_path / "gone.pdf"
    _make_digital_pdf(source)
    stage(store, job_id, document_id, source)
    job_request = {
        "job_id": job_id,
        "documents": [{"document_id": document_id, "extension": ".pdf", "display_name": "gone.pdf"}],
    }

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue=CPU_QUEUE, workflows=[DocumentIngestionWorkflow],
                activities=[planning_with_vanished_input, route_page_activity, audit_page_activity, confirm_review_activity, digital_page_activity,
                            native_document_activity, dispatch_webhook_activity],
            ):
                handle = await env.client.start_workflow(
                    DocumentIngestionWorkflow.run, args=[job_request],
                    id=f"doc-extract-{job_id}", task_queue=CPU_QUEUE,
                )
                return await handle.result()

    result = asyncio.run(scenario())

    assert result["status"] == "failed"
    assert result["delivered_pages"] == 0
    assert result["error"]


def test_page_activity_cannot_return_a_different_page(store, tmp_path):
    job_id = "5" * 32
    document_id = "e" * 32
    source = tmp_path / "identity.pdf"
    _make_digital_pdf(source)
    stage(store, job_id, document_id, source)

    @temporal_activity.defn(name="digital_page_activity")
    async def wrong_page(payload):
        return {
            "document_id": payload["document_id"], "display_name": payload["display_name"],
            "extension": payload["extension"], "page_number": payload["page_number"] + 1,
            "total_pages": payload["total_pages"], "classification": "digital",
            "status": "completed", "audit": {"is_valid": True, "requires_hitl": False},
            "confidence": 1.0,
        }

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue=CPU_QUEUE, workflows=[DocumentIngestionWorkflow],
                activities=[plan_document_activity, route_page_activity, audit_page_activity,
                            confirm_review_activity, wrong_page, native_document_activity],
            ):
                handle = await env.client.start_workflow(
                    DocumentIngestionWorkflow.run,
                    args=[{"job_id": job_id, "documents": [{"document_id": document_id,
                           "extension": ".pdf", "display_name": source.name}]}],
                    id=f"doc-extract-{job_id}", task_queue=CPU_QUEUE,
                )
                return await handle.result()

    result = asyncio.run(scenario())
    assert result["status"] == "failed"
    assert result["page_outcomes"][0]["status"] == "failed"


def test_review_wait_and_signal(store, tmp_path, monkeypatch):
    from ingestion_pipeline.extraction import auditor as auditor_module

    @temporal_activity.defn(name="confirm_review_activity")
    async def committed_review(_payload):
        return True

    class FakeReport:
        def summary_dict(self):
            return {"is_valid": False, "overall_confidence": 0.4,
                    "requires_hitl": True, "issue_count": 2, "issues": ["low"]}

    monkeypatch.setattr(auditor_module, "run_document_audit", lambda **kwargs: FakeReport())

    job_id = "4" * 32
    document_id = "d" * 32
    source = tmp_path / "rfq.pdf"
    _make_digital_pdf(source)
    stage(store, job_id, document_id, source)
    job_request = {
        "job_id": job_id,
        "documents": [{"document_id": document_id, "extension": ".pdf", "display_name": "rfq.pdf"}],
    }

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue=CPU_QUEUE, workflows=[DocumentIngestionWorkflow],
                activities=[plan_document_activity, route_page_activity, audit_page_activity, committed_review, digital_page_activity,
                            native_document_activity, dispatch_webhook_activity],
            ):
                handle = await env.client.start_workflow(
                    DocumentIngestionWorkflow.run, args=[job_request],
                    id=f"doc-extract-{job_id}", task_queue=CPU_QUEUE,
                )
                for _ in range(50):
                    status = await handle.query(DocumentIngestionWorkflow.get_status)
                    if status["status"] == "waiting_for_human_review":
                        break
                else:
                    pytest.fail("workflow never paused for review")
                assert status["job_id"] == job_id
                assert status["artifact_refs"]
                await handle.signal(DocumentIngestionWorkflow.submit_human_review, {"result_version": 0})
                return await handle.result()

    result = asyncio.run(scenario())

    assert result["status"] == "completed"
    assert result["human_reviewed"] is True
