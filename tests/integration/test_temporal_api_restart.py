"""Real ASGI admission and polling across a Temporal worker restart."""

import asyncio
import hashlib
import json

import pymupdf
from fastapi.testclient import TestClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ingestion_pipeline import api
from ingestion_pipeline.extraction import auditor as auditor_module
from ingestion_pipeline.job_store import JobStore
from ingestion_pipeline.storage.artifacts import ArtifactStore
from ingestion_pipeline.temporal import activities, bridge
from ingestion_pipeline.temporal import client as temporal_client
from ingestion_pipeline.temporal.workflows import DocumentIngestionWorkflow


def test_api_review_survives_worker_restart(tmp_path, monkeypatch):
    key = "a" * 40
    credentials = {hashlib.sha256(key.encode()).hexdigest(): {
        "tenant_id": "tenant", "subject": "admin", "roles": ["admin", "extract", "review"],
    }}
    monkeypatch.setenv("INGESTION_API_KEY_SHA256_JSON", json.dumps(credentials))
    monkeypatch.setattr(api, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(activities, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    store = JobStore(tmp_path / "jobs.db")
    artifacts = ArtifactStore.from_output_root(tmp_path)
    monkeypatch.setattr(api, "job_store", store)
    monkeypatch.setattr(activities, "job_store", store)
    monkeypatch.setattr(activities, "STORE", artifacts)
    monkeypatch.setattr(bridge, "STORE", artifacts)
    monkeypatch.setattr(bridge, "_job_store", store)
    monkeypatch.setattr(api, "feature_enabled", lambda name: name == "ENABLE_TEMPORAL_TEST_LANE")

    async def idle_reconciler():
        await asyncio.Event().wait()

    monkeypatch.setattr(bridge, "run_reconciler", idle_reconciler)

    class NeedsReview:
        def summary_dict(self):
            return {"is_valid": False, "requires_hitl": True, "issue_count": 1,
                    "overall_confidence": 0.5}

    monkeypatch.setattr(auditor_module, "run_document_audit", lambda **_: NeedsReview())
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "One digital document requiring review")
        body = pdf.tobytes()

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async def start(request):
                return (await env.client.start_workflow(
                    DocumentIngestionWorkflow.run, args=[request],
                    id=f"doc-extract-{request['job_id']}", task_queue=activities.CPU_QUEUE,
                )).id

            async def query(job_id):
                return await env.client.get_workflow_handle(f"doc-extract-{job_id}").query(
                    DocumentIngestionWorkflow.get_status)

            bridge.set_workflow_starter(start)
            bridge.set_workflow_status_querier(query)
            cpu_activities = [
                activities.plan_document_activity, activities.route_page_activity,
                activities.digital_page_activity, activities.native_document_activity,
                activities.audit_page_activity, activities.confirm_review_activity,
            ]
            with TestClient(api.app) as http:
                auth = {"Authorization": f"Bearer {key}"}
                admitted = http.post(
                    "/extract/v1", headers=auth, data={"engine": "temporal_test"},
                    files={"files": ("review.pdf", body, "application/pdf")},
                )
                assert admitted.status_code == 202, admitted.text
                job_id = admitted.json()["job_id"]
                assert await store.claim_next("legacy-worker") is None

                async with Worker(env.client, task_queue=activities.CPU_QUEUE,
                                  workflows=[DocumentIngestionWorkflow], activities=cpu_activities,
                                  max_cached_workflows=0):
                    assert await bridge.reconcile_pending_submissions() == 1
                    for _ in range(80):
                        state = await query(job_id)
                        if state["status"] == "waiting_for_human_review":
                            break
                        await asyncio.sleep(0.05)
                    else:
                        raise AssertionError("Workflow did not reach durable review")

                # The first worker stops while the workflow is durably paused.
                # A new worker replays it and serves the real status query.
                async with Worker(env.client, task_queue=activities.CPU_QUEUE,
                                  workflows=[DocumentIngestionWorkflow], activities=cpu_activities,
                                  max_cached_workflows=0):
                    assert await bridge.reconcile_pending_results() == 1
                    waiting = http.get(f"/extract/v1/{job_id}", headers=auth)
                    assert waiting.json()["status"] == "waiting_for_review"
                    assert "One digital document" in waiting.json()["result"]["documents"]["review.pdf"]["content"]

                    # The review is committed before the reconciler signals
                    # the workflow, so this also exercises signal replay.
                    reviewed = http.post(
                        f"/extract/v1/{job_id}/review", headers=auth,
                        json={"document_name": "review.pdf", "result_version": 0, "corrections": []},
                    )
                    assert reviewed.status_code == 200, reviewed.text
                    assert reviewed.json()["status"] == "completed"

                    async def signal(workflow_id, payload):
                        await env.client.get_workflow_handle(workflow_id).signal(
                            DocumentIngestionWorkflow.submit_human_review, payload)

                    monkeypatch.setattr(temporal_client, "send_human_review_signal", signal)
                    assert await bridge.reconcile_pending_reviews() == 1
                    result = await env.client.get_workflow_handle(f"doc-extract-{job_id}").result()
                assert result["status"] == "completed"
                assert result["human_reviewed"] is True
                assert await bridge.reconcile_pending_reviews() == 0
                assert http.get(f"/extract/v1/{job_id}", headers=auth).json()["review_version"] == 1

    try:
        asyncio.run(scenario())
    finally:
        bridge.set_workflow_starter(None)
        bridge.set_workflow_status_querier(None)
