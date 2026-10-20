"""ASGI admission keeps Temporal jobs out of the legacy worker queue."""

import asyncio
import hashlib
import json

import pymupdf
from fastapi.testclient import TestClient

from ingestion_pipeline import api
from ingestion_pipeline.job_store import JobStore
from ingestion_pipeline.temporal import bridge


def test_admin_temporal_admission_is_atomic_and_cancellable(tmp_path, monkeypatch):
    admin_key = "a" * 40
    owner_key = "o" * 40
    credentials = {
        hashlib.sha256(admin_key.encode()).hexdigest(): {
            "tenant_id": "tenant", "subject": "admin", "roles": ["admin", "extract"],
        },
        hashlib.sha256(owner_key.encode()).hexdigest(): {
            "tenant_id": "tenant", "subject": "owner", "roles": ["extract"],
        },
    }
    monkeypatch.setenv("INGESTION_API_KEY_SHA256_JSON", json.dumps(credentials))
    monkeypatch.setattr(api, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(api, "job_store", store)

    async def idle_reconciler():
        await asyncio.Event().wait()

    monkeypatch.setattr(bridge, "run_reconciler", idle_reconciler)
    monkeypatch.setattr(api, "feature_enabled", lambda name: name == "ENABLE_TEMPORAL_TEST_LANE")

    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Temporal admission test")
        body = pdf.tobytes()

    with TestClient(api.app) as client:
        files = {"files": ("invoice.pdf", body, "application/pdf")}
        owner = client.post("/extract/v1", headers={"Authorization": f"Bearer {owner_key}"},
                            files=files, data={"engine": "temporal_test"})
        assert owner.status_code == 403
        accepted = client.post("/extract/v1", headers={"Authorization": f"Bearer {admin_key}"},
                               files=files, data={"engine": "temporal_test"})
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        assert len(job_id) == 32
        outbox = asyncio.run(store.submission_for_job(job_id))
        assert outbox["scheduler_kind"] == "temporal_test"
        assert outbox["request"]["documents"][0]["artifact"]["sha256"] == hashlib.sha256(body).hexdigest()
        assert asyncio.run(store.claim_next("legacy-worker")) is None
        cancelled = client.delete(f"/extract/v1/{job_id}", headers={"Authorization": f"Bearer {admin_key}"})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert asyncio.run(store.pending_lane_submissions("temporal_test")) == []
