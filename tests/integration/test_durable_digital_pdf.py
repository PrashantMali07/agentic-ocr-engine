"""Real ASGI upload, durable queue, and native digital-PDF extraction."""

import asyncio
import hashlib
import json

import pymupdf
from fastapi.testclient import TestClient

from ingestion_pipeline import api
from ingestion_pipeline.job_store import JobStore
from ingestion_pipeline.services import job_runner


def test_real_pdf_runs_through_durable_api_and_remains_tenant_scoped(tmp_path, monkeypatch):
    owner_key = "o" * 40
    other_key = "x" * 40
    credentials = {
        hashlib.sha256(owner_key.encode()).hexdigest(): {
            "tenant_id": "owner", "subject": "service", "roles": ["extract"],
        },
        hashlib.sha256(other_key.encode()).hexdigest(): {
            "tenant_id": "other", "subject": "service", "roles": ["extract"],
        },
    }
    monkeypatch.setenv("INGESTION_API_KEY_SHA256_JSON", json.dumps(credentials))
    monkeypatch.delenv("OCR_MODEL_MANIFEST", raising=False)
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(api, "job_store", store)
    monkeypatch.setattr(api, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(job_runner, "job_store", store)
    monkeypatch.setattr(job_runner, "DEFAULT_OUTPUT_DIR", str(tmp_path))

    document = pymupdf.open()
    page = document.new_page()
    marker = "REAL DIGITAL PDF ACCOUNT REFERENCE ABC123"
    page.insert_text((72, 72), marker)
    page.insert_text((72, 96), "This invoice contains enough visible words for native PDF routing.")
    page.insert_text((72, 120), "The parser should preserve this digital text without calling OCR.")
    pdf_bytes = document.tobytes()
    document.close()

    with TestClient(api.app) as client:
        submitted = client.post(
            "/extract/v1", headers={"Authorization": f"Bearer {owner_key}"},
            files={"files": ("invoice.pdf", pdf_bytes, "application/pdf")},
        )
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]
        assert client.get(f"/extract/v1/{job_id}", headers={
            "Authorization": f"Bearer {other_key}",
        }).status_code == 404
        assert asyncio.run(store.claim_next("integration-worker"))[0] == job_id
        assert job_runner.run(job_id, "integration-worker") == 0
        result = client.get(f"/extract/v1/{job_id}", headers={
            "Authorization": f"Bearer {owner_key}",
        })
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "completed"
        assert marker in body["result"]["documents"]["invoice.pdf"]["content"]
