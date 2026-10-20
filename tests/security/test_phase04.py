import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from ingestion_pipeline import api
from ingestion_pipeline.extraction import hybrid
from ingestion_pipeline.graph import workflow as graph_workflow
from ingestion_pipeline.job_store import JobStore, ReviewConflict
from ingestion_pipeline.schema import ExtractResponse, JobStatus, ReviewRequest
from ingestion_pipeline.security.auth import Principal
from ingestion_pipeline.services import job_runner, monitoring, worker
from scripts.evaluate_phase04_acceptance import edit_distance, load_ground_truth, score_documents, value_present

OWNER = Principal("tenant-a", "owner", frozenset({"extract"}))
REVIEWER = Principal("tenant-a", "reviewer", frozenset({"review"}))
FOREIGN = Principal("tenant-b", "reviewer", frozenset({"review"}))


def result(two_documents=False):
    documents = {"one.pdf": {"content": "Total 12", "correction_suggestions": [{"original": "12", "proposed": "13"}]}}
    if two_documents:
        documents["two.pdf"] = {"content": "Date 2026", "audit": {"requires_hitl": True}}
    return ExtractResponse(request_id="job", documents=documents)


def request(name="one.pdf", version=0, corrections=None):
    return ReviewRequest(document_name=name, result_version=version, corrections=corrections or [])


def test_review_is_scoped_atomic_and_audited(tmp_path):
    async def run():
        db_path = tmp_path / "jobs.db"
        store = JobStore(db_path)
        await store.create("job", OWNER)
        await store.update("job", status=JobStatus.waiting_for_review, result=result(two_documents=True))
        correction = {"start": 6, "end": 8, "expected": "12", "replacement": "13"}
        first = request(corrections=[correction])
        assert await store.submit_review("job", FOREIGN, first) is None
        with pytest.raises(PermissionError):
            await store.submit_review("job", OWNER, first)
        with pytest.raises(ReviewConflict):
            await store.submit_review("job", REVIEWER, request(corrections=[{
                **correction, "expected": "99"}]))
        accepted = await store.submit_review("job", REVIEWER, first)
        assert accepted.status == JobStatus.waiting_for_review
        assert accepted.review_version == 1
        assert accepted.result.documents["one.pdf"].content == "Total 13"
        assert accepted.result.documents["two.pdf"].content == "Date 2026"
        with pytest.raises(ReviewConflict):
            await store.submit_review("job", REVIEWER, first)
        store = JobStore(db_path)  # A new process sees the pending review and audit.
        finished = await store.submit_review("job", REVIEWER, request("two.pdf", 1))
        assert finished.status == JobStatus.completed
        assert finished.review_version == 2
        assert finished.reviewed_by == "reviewer"
        assert len(await store.review_history("job", REVIEWER)) == 2
        assert await store.review_history("job", FOREIGN) is None
        with pytest.raises(PermissionError):
            await store.review_history("job", OWNER)
        with sqlite3.connect(db_path) as db:
            rows = db.execute("SELECT before_result, after_result FROM review_events ORDER BY version").fetchall()
        assert len(rows) == 2 and "Total 12" in rows[0][0] and "Total 13" in rows[0][1]

        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        await store.record_job_observation("job", 1, 3.2, 1024, None, "completed")
        with sqlite3.connect(db_path) as db:
            value = json.loads(db.execute("SELECT data FROM jobs WHERE job_id = 'job'").fetchone()[0])
            value["updated_at"] = old
            db.execute("UPDATE jobs SET data = ?, updated_at = ? WHERE job_id = 'job'",
                       (json.dumps(value), old))
        assert await store.get_for_principal("job", REVIEWER) is None
        assert await store.purge_expired() == 1
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM review_events WHERE job_id = 'job'").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM job_observations WHERE job_id = 'job'").fetchone()[0] == 0

    asyncio.run(run())


def test_worker_commits_review_required_result(tmp_path, monkeypatch):
    output = tmp_path / "output"
    attachments = output / "jobs" / "job" / "attachments"
    attachments.mkdir(parents=True)
    document_id = "a" * 32
    (attachments / f"{document_id}.pdf").write_bytes(b"%PDF-1.7")
    store = JobStore(output / "jobs.db")
    monkeypatch.setattr(job_runner, "job_store", store)
    monkeypatch.setattr(job_runner, "DEFAULT_OUTPUT_DIR", str(output))
    monkeypatch.setattr(worker, "DEFAULT_OUTPUT_DIR", str(output))
    monkeypatch.setattr(job_runner, "process_email_attachments", lambda **kwargs: {
        "email_id": "job",
        "attachments": [{"file": f"{document_id}.pdf", "format": "pdf", "pages": 1}],
        "blocks": [{"source_file": f"{document_id}.pdf", "order": 0, "page": 1,
                    "content": "Total 12", "suggested_content": "Total 13"}],
    })

    async def prepare():
        await store.admit("job", OWNER, [{"document_id": document_id,
            "original_name": "one.pdf", "size": 8}], 8)
        assert await store.claim_next("worker")
    asyncio.run(prepare())
    assert job_runner.run("job", "worker") == 0
    job = asyncio.run(store.get("job"))
    assert job.status == JobStatus.waiting_for_review
    assert job.result.documents["one.pdf"].content == "Total 12"
    assert job.progress.total == job.progress.processed == 1
    asyncio.run(worker.cleanup_terminal_artifacts(store))
    assert not attachments.parent.exists()
    assert asyncio.run(store.get("job")).status == JobStatus.waiting_for_review


def test_review_rejects_overlapping_or_markup_edits(tmp_path):
    async def run():
        store = JobStore(tmp_path / "jobs.db")
        await store.create("job", OWNER)
        await store.update("job", status=JobStatus.waiting_for_review, result=result())
        with pytest.raises(ReviewConflict):
            await store.submit_review("job", REVIEWER, request(corrections=[
                {"start": 0, "end": 5, "expected": "Total", "replacement": "Grand"},
                {"start": 2, "end": 4, "expected": "ta", "replacement": "xx"},
            ]))
        assert (await store.get("job")).review_version == 0
    asyncio.run(run())


def test_retention_preserves_active_and_unremoved_artifacts(tmp_path):
    async def run():
        db_path = tmp_path / "jobs.db"
        store = JobStore(db_path)
        for job_id, state in [("active", JobStatus.processing), ("terminal", JobStatus.completed),
                              ("review", JobStatus.waiting_for_review), ("artifacts", JobStatus.failed)]:
            await store.create(job_id, OWNER)
            await store.update(job_id, status=state)
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE jobs SET updated_at = ?", (old,))
            db.execute("UPDATE jobs SET manifest = '[]' WHERE job_id = 'artifacts'")
        # API reads use the serialized job timestamp, as do real worker updates.
        with sqlite3.connect(db_path) as db:
            for job_id in ("active", "terminal", "review", "artifacts"):
                data = db.execute("SELECT data FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
                value = json.loads(data)
                value["updated_at"] = old
                db.execute("UPDATE jobs SET data = ?, updated_at = ? WHERE job_id = ?",
                           (json.dumps(value), old, job_id))
        assert await store.get_for_principal("active", OWNER) is not None
        assert await store.get_for_principal("terminal", OWNER) is None
        assert await store.get_for_principal("review", REVIEWER) is None
        assert await store.purge_expired() == 2
        assert await store.get("active") is not None
        assert await store.get("artifacts") is not None
    asyncio.run(run())


def test_review_http_contract_and_tenant_scope(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")

    async def prepare():
        await store.create("job", OWNER)
        await store.update("job", status=JobStatus.waiting_for_review, result=result())
    asyncio.run(prepare())

    reviewer_key = "r" * 40
    foreign_key = "f" * 40
    credentials = {
        hashlib.sha256(reviewer_key.encode()).hexdigest(): {
            "tenant_id": "tenant-a", "subject": "reviewer", "roles": ["review"]},
        hashlib.sha256(foreign_key.encode()).hexdigest(): {
            "tenant_id": "tenant-b", "subject": "reviewer", "roles": ["review"]},
    }
    monkeypatch.setenv("INGESTION_API_KEY_SHA256_JSON", json.dumps(credentials))
    monkeypatch.setattr(api, "job_store", store)
    payload = {"document_name": "one.pdf", "result_version": 0,
               "corrections": [{"start": 6, "end": 8, "expected": "12", "replacement": "13"}]}
    with TestClient(api.app) as client:
        assert client.post("/extract/v1/job/review", headers={"Authorization": f"Bearer {foreign_key}"},
                           json=payload).status_code == 404
        response = client.post("/extract/v1/job/review", headers={"Authorization": f"Bearer {reviewer_key}"},
                               json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["documents"]["one.pdf"]["content"] == "Total 13"
        assert client.post("/extract/v1/job/review", headers={"Authorization": f"Bearer {reviewer_key}"},
                           json=payload).status_code == 409
        history = client.get("/extract/v1/job/reviews", headers={"Authorization": f"Bearer {reviewer_key}"})
        assert history.status_code == 200 and len(history.json()) == 1


def test_operational_metrics_are_admin_only_and_readiness_needs_worker(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(api, "job_store", store)
    admin_key = "a" * 40
    review_key = "r" * 40
    monkeypatch.setenv("INGESTION_API_KEY_SHA256_JSON", json.dumps({
        hashlib.sha256(admin_key.encode()).hexdigest(): {
            "tenant_id": "tenant-a", "subject": "operator", "roles": ["admin"]},
        hashlib.sha256(review_key.encode()).hexdigest(): {
            "tenant_id": "tenant-a", "subject": "reviewer", "roles": ["review"]},
    }))
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.get("/operations/metrics", headers={"Authorization": f"Bearer {review_key}"}).status_code == 403
        asyncio.run(store.touch_worker())
        assert client.get("/ready").status_code == 200
        response = client.get("/operations/metrics", headers={"Authorization": f"Bearer {admin_key}"})
        assert response.status_code == 200
        assert response.json()["worker_last_seen_seconds"] is not None


def test_alerts_and_observations_are_durable_without_document_text(tmp_path):
    async def run():
        store = JobStore(tmp_path / "jobs.db")
        await store.create("queued", OWNER)
        await store.touch_worker()
        await store.record_job_observation("queued", 1, 12.5, 4096, 8 * 1024 * 1024,
                                           "failed", peak_artifact_bytes=123)
        old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        with sqlite3.connect(tmp_path / "jobs.db") as db:
            db.execute("UPDATE jobs SET updated_at = ? WHERE job_id = 'queued'", (old,))
        snapshot = await JobStore(tmp_path / "jobs.db").operational_snapshot()
        assert "queue_stuck" in snapshot["alerts"]
        assert snapshot["max_duration_seconds"] == 12.5
        assert snapshot["peak_rss_bytes"] == 4096
        assert snapshot["peak_gpu_bytes"] == 8 * 1024 * 1024
        assert snapshot["peak_artifact_bytes"] == 123
        assert snapshot["attempt_counts"]["failed"] == 1
        assert "Total 12" not in json.dumps(snapshot)
    asyncio.run(run())


def test_cleanup_failure_is_counted_then_recovered(tmp_path, monkeypatch, caplog):
    output = tmp_path / "output"
    monkeypatch.setattr(worker, "DEFAULT_OUTPUT_DIR", str(output))
    store = JobStore(tmp_path / "jobs.db")

    async def prepare():
        path = output / "jobs" / "finished"
        path.mkdir(parents=True)
        await store.admit("finished", OWNER, [{"document_id": "a" * 32,
            "original_name": "one.pdf", "size": 8}], 8)
        assert await store.cancel("finished", OWNER)
    asyncio.run(prepare())
    real_rmtree = worker.shutil.rmtree
    monkeypatch.setattr(worker.shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError("fixture secret")))
    asyncio.run(worker.cleanup_terminal_artifacts(store))
    snapshot = asyncio.run(store.operational_snapshot())
    assert snapshot["cleanup_failure_count"] == 1
    assert "artifact_cleanup_failed" in snapshot["alerts"]
    assert snapshot["reserved_bytes"] == 0  # Finished artifacts do not consume active admission capacity.
    assert "fixture secret" not in caplog.text
    monkeypatch.setattr(worker.shutil, "rmtree", real_rmtree)
    asyncio.run(worker.cleanup_terminal_artifacts(store))
    assert "artifact_cleanup_failed" not in asyncio.run(store.operational_snapshot())["alerts"]


def test_alert_transitions_are_logged_once(tmp_path, monkeypatch, caplog):
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(worker, "_last_alerts", set())
    caplog.set_level(logging.INFO, logger="ingest.worker")

    async def run():
        await store.create("queued", OWNER)
        await store.touch_worker()
        old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        with sqlite3.connect(tmp_path / "jobs.db") as db:
            db.execute("UPDATE jobs SET updated_at = ? WHERE job_id = 'queued'", (old,))
        await worker.log_alert_changes(store)
        await worker.log_alert_changes(store)
        assert caplog.text.count("Operational alert active: queue_stuck") == 1
        with sqlite3.connect(tmp_path / "jobs.db") as db:
            db.execute("UPDATE jobs SET updated_at = ? WHERE job_id = 'queued'",
                       (datetime.now(timezone.utc).isoformat(),))
        await worker.log_alert_changes(store)
        assert caplog.text.count("Operational alert cleared: queue_stuck") == 1
    asyncio.run(run())


def test_process_sampling_is_bounded_and_optional(monkeypatch, tmp_path):
    assert monitoring.peak_rss_bytes(os.getpid()) > 0
    fake = subprocess.CompletedProcess([], 0, "42, 10\n7, 3\n42, 2\n", "")
    monkeypatch.setattr(monitoring.subprocess, "run", lambda *args, **kwargs: fake)
    assert monitoring._gpu_memory_bytes(42) == 12 * 1024 * 1024
    assert monitoring._gpu_memory_bytes(99) is None
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "input.pdf").write_bytes(b"abc")
    (tmp_path / "outside").write_bytes(b"x" * 100)
    (artifacts / "link").symlink_to(tmp_path / "outside")
    assert monitoring.artifact_bytes(artifacts) == 3


def test_worker_commit_before_child_exit_is_observed_as_completed(tmp_path):
    async def run():
        db_path = tmp_path / "jobs.db"
        store = JobStore(db_path)
        await store.admit("job", OWNER, [{"document_id": "a" * 32,
            "original_name": "one.pdf", "size": 8}], 8)
        child = (
            "import json, sqlite3, sys, time; "
            "db=sqlite3.connect(sys.argv[1]); "
            "row=db.execute('SELECT data FROM jobs WHERE job_id=?',(sys.argv[2],)).fetchone(); "
            "data=json.loads(row[0]); data['status']='completed'; "
            "db.execute('UPDATE jobs SET data=?, status=? WHERE job_id=?',"
            "(json.dumps(data),'completed',sys.argv[2])); db.commit(); db.close(); time.sleep(1.5)"
        )
        assert await worker.run_once(
            store, timeout_seconds=5, runner_command=[sys.executable, "-c", child, str(db_path)],
        )
        snapshot = await store.operational_snapshot()
        assert snapshot["attempt_counts"] == {"completed": 1}
    asyncio.run(run())


def test_development_graph_discards_checkpoint_and_never_returns_interrupt_as_final(monkeypatch):
    deleted = []
    graphs = []

    class Saver:
        def delete_thread(self, thread_id):
            deleted.append(thread_id)

    class Graph:
        def __init__(self, interrupted):
            self.checkpointer = Saver()
            self.interrupted = interrupted

        def invoke(self, state, config):
            if self.interrupted:
                return {"__interrupt__": ["review"], "final_result": {"status": "success"}}
            return {"final_result": {"status": "success", "clean_text": "ok"}}

    def make_graph():
        graph = Graph(interrupted=len(graphs) == 0)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(graph_workflow, "build_document_ingestion_graph", make_graph)
    with pytest.raises(graph_workflow.ReviewRequired):
        graph_workflow.run_document_graph("unused.pdf", thread_id="first")
    assert graph_workflow.run_document_graph("unused.pdf", thread_id="second")["clean_text"] == "ok"
    assert len(graphs) == 2 and deleted == ["first", "second"]


def test_sync_compatibility_route_reports_review_required(tmp_path, monkeypatch):
    from ingestion_pipeline.graph import workflow

    key = "g" * 40
    monkeypatch.setenv("INGESTION_API_KEY_SHA256_JSON", json.dumps({
        hashlib.sha256(key.encode()).hexdigest(): {
            "tenant_id": "tenant-a", "subject": "owner", "roles": ["extract"]},
    }))
    monkeypatch.setattr(api, "feature_enabled", lambda name: name in {
        "ENABLE_SYNC_EXTRACTION", "ENABLE_LANGGRAPH_ENGINE"})
    monkeypatch.setattr(workflow, "run_document_graph", lambda *args, **kwargs: (
        (_ for _ in ()).throw(workflow.ReviewRequired("review"))))
    with TestClient(api.app) as client:
        response = client.post(
            "/extract", headers={"Authorization": f"Bearer {key}"},
            data={"engine": "langgraph"},
            files={"files": ("one.pdf", b"%PDF-1.7\nfixture", "application/pdf")},
        )
    assert response.status_code == 409
    assert "Human review required" in response.json()["detail"]


def test_tesseract_error_log_does_not_include_parser_message(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture secret document text")

    monkeypatch.setattr(hybrid.pytesseract, "image_to_data", fail)
    assert hybrid.extract_tesseract_lines(np.zeros((5, 5, 3), dtype=np.uint8)) == []
    assert "fixture secret document text" not in caplog.text


def test_accuracy_gate_requires_independent_full_transcripts(tmp_path, monkeypatch):
    from scripts import evaluate_phase04_acceptance

    monkeypatch.setattr(evaluate_phase04_acceptance, "ROOT", tmp_path)
    source = tmp_path / "test_asset" / "RFQ_06_scanned.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.7\nindependent fixture bytes")
    unverified = tmp_path / "unverified.json"
    unverified.write_text(json.dumps({
        "documents": {"RFQ_06_scanned.pdf": {"fields": {"id": "ABC"}}},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="independent reviewer"):
        load_ground_truth(unverified)
    verified = {
        "verified_by": "Independent reviewer", "verified_at": "2026-10-19",
        "documents": {"RFQ_06_scanned.pdf": {
            "sha256": "0" * 64, "transcript": "Full transcript", "fields": {"id": "ABC"},
        }},
    }
    unverified.write_text(json.dumps(verified), encoding="utf-8")
    with pytest.raises(ValueError, match="digest does not match"):
        load_ground_truth(unverified)
    verified["documents"]["RFQ_06_scanned.pdf"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    unverified.write_text(json.dumps(verified), encoding="utf-8")
    assert "RFQ_06_scanned.pdf" in load_ground_truth(unverified)
    labels = {"scan.pdf": {"transcript": "Invoice A12 Total 10",
                           "fields": {"id": "A12", "amount": "10"}}}
    exact = score_documents(labels, {"scan.pdf": "Invoice A12 Total 10"})
    assert exact["cer"] == exact["wer"] == 0
    assert exact["field_value_presence"] == 1
    changed = score_documents(labels, {"scan.pdf": "Invoice A12 Total 11"})
    assert changed["cer"] > 0 and changed["wer"] > 0
    assert changed["documents"]["scan.pdf"]["missing_field_names"] == ["amount"]
    assert not value_present("10", "Total 101")
    assert value_present("RFQ/2026/1006", "Reference RFQ20261006")
    table = score_documents(
        {"scan.pdf": {"transcript": "Total 10", "fields": {"total": "10"}}},
        {"scan.pdf": "<table><tr><td>Total</td><td>10</td></tr></table>"},
    )
    assert table["cer"] == 0 and table["wer"] == 0
    assert table["field_value_presence"] == 1
    assert edit_distance("kitten", "sitting") == 3


def test_observation_schema_migrates_previous_phase4_database(tmp_path):
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE job_observations (job_id TEXT NOT NULL, attempt INTEGER NOT NULL, "
            "duration_seconds REAL NOT NULL, peak_rss_bytes INTEGER, peak_gpu_bytes INTEGER, "
            "exit_kind TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY (job_id, attempt))"
        )
    snapshot = asyncio.run(JobStore(db_path).operational_snapshot())
    assert snapshot["peak_artifact_bytes"] is None
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(job_observations)")}
    assert "peak_artifact_bytes" in columns
