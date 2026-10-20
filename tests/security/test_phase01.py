import asyncio
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ingestion_pipeline import api
from ingestion_pipeline.core.safe_html import sanitize_tables
from ingestion_pipeline.job_store import JobStore
from ingestion_pipeline.security.auth import Principal
from ingestion_pipeline.security.body_limit import RequestBodyLimit
from ingestion_pipeline.simulator.db import _spreadsheet_safe
from ingestion_pipeline.simulator.report import generate_html_dashboard
from ingestion_pipeline.storage.uploads import validate_name


@pytest.fixture
def client(tmp_path, monkeypatch):
    keys = {
        hashlib.sha256(b"owner-secret".ljust(40, b"x")).hexdigest(): {
            "tenant_id": "alpha", "subject": "owner", "roles": ["extract"]
        },
        hashlib.sha256(b"other-secret".ljust(40, b"x")).hexdigest(): {
            "tenant_id": "alpha", "subject": "other", "roles": ["extract"]
        },
        hashlib.sha256(b"tenant-secret".ljust(40, b"x")).hexdigest(): {
            "tenant_id": "beta", "subject": "owner", "roles": ["extract", "review"]
        },
    }
    monkeypatch.setenv("INGESTION_API_KEY_SHA256_JSON", json.dumps(keys))
    monkeypatch.setattr(api, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(api, "job_store", JobStore(tmp_path / "jobs.db"))

    def fake_extract(*, file_paths, email_id, **kwargs):
        return {
            "email_id": email_id,
            "attachments": [{"file": path.name, "format": "pdf", "pages": 1} for path in file_paths],
            "blocks": [{"source_file": path.name, "order": 0, "page": 1, "content": "sample"} for path in file_paths],
        }

    monkeypatch.setattr(api, "process_email_attachments", fake_extract)
    with TestClient(api.app) as test_client:
        yield test_client


def auth(key):
    return {"Authorization": "Bearer " + key.ljust(40, "x")}


def pdf(name="sample.pdf", data=b"%PDF-1.7\nsynthetic"):
    return {"files": (name, data, "application/pdf")}


def test_fail_closed_auth_and_disabled_routes(client):
    assert client.post("/extract/v1", files=pdf()).status_code == 401
    assert client.post("/extract-dir", headers=auth("owner-secret"), data={"directory_path": "/tmp"}).status_code == 503
    assert client.post("/batch-extract", headers=auth("owner-secret"), json={"directories": [{"directory_path": "/tmp"}]}).status_code == 503
    assert client.post("/extract", headers=auth("owner-secret"), files=pdf()).status_code == 503
    assert client.post("/extract/v1", headers=auth("owner-secret"), files=pdf(), data={"callback_url": "http://127.0.0.1/"}).status_code == 400
    assert client.post("/extract/v1", headers=auth("owner-secret"), files=pdf(), data={"save_visualizations": "true"}).status_code == 400
    assert client.post("/extract/v1", headers=auth("owner-secret"), files=pdf(), data={"engine": "temporal"}).status_code == 400


@pytest.mark.parametrize("name", ["/tmp/escape.pdf", "../escape.pdf", "sub/file.pdf", "same\\file.pdf"])
def test_upload_filename_escape_rejected(client, name):
    assert client.post("/extract/v1", headers=auth("owner-secret"), files=pdf(name)).status_code == 400


def test_nul_filename_rejected_by_storage():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        validate_name("a\x00.pdf")


def test_upload_content_and_duplicate_names(client):
    assert client.post("/extract/v1", headers=auth("owner-secret"), files=pdf(data=b"not a PDF")).status_code == 400
    files = [("files", ("one.pdf", b"%PDF-1.7", "application/pdf")), ("files", ("ONE.PDF", b"%PDF-1.7", "application/pdf"))]
    assert client.post("/extract/v1", headers=auth("owner-secret"), files=files).status_code == 400


def test_owner_scope_and_display_name(client):
    submitted = client.post("/extract/v1", headers=auth("owner-secret"), files=pdf("invoice.pdf"))
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    response = client.get(f"/extract/v1/{job_id}", headers=auth("owner-secret"))
    assert response.json()["status"] == "queued", response.text
    assert client.get(f"/extract/v1/{job_id}", headers=auth("other-secret")).status_code == 404
    assert client.get(f"/extract/v1/{job_id}", headers=auth("tenant-secret")).status_code == 404
    assert client.post(f"/extract/v1/{job_id}/review", headers=auth("owner-secret"), json={}).status_code == 403
    assert client.post(f"/extract/v1/{job_id}/review", headers=auth("tenant-secret"),
                       json={"document_name": "invoice.pdf", "result_version": 0}).status_code == 404
    assert client.delete(f"/extract/v1/{job_id}", headers=auth("other-secret")).status_code == 404
    cancelled = client.delete(f"/extract/v1/{job_id}", headers=auth("owner-secret"))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_request_body_limit(client):
    over = b"%PDF-1.7" + b"x" * (30 * 1024 * 1024 + 256 * 1024)
    response = client.post("/extract/v1", headers=auth("owner-secret"), files=pdf(data=over))
    assert response.status_code == 413, response.text


def test_old_job_rows_are_quarantined(tmp_path):
    legacy = tmp_path / "older.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)")
        db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (
            "legacy-id",
            json.dumps({"job_id": "legacy-id", "status": "queued", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}),
            "2026-01-01T00:00:00Z",
        ))

    async def run():
        store = JobStore(legacy)
        owner = Principal("alpha", "owner", frozenset({"extract"}))
        await store.create("new-id", owner)
        assert await store.get_for_principal("new-id", owner)
        assert await store.get_for_principal("legacy-id", owner) is None
        assert await store.get_for_principal("new-id", Principal("alpha", "other", frozenset({"extract"}))) is None
    asyncio.run(run())


def test_streaming_body_without_length_is_bounded():
    async def consume(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run():
        chunks = iter([
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ])
        sent = []

        async def receive():
            return next(chunks)

        async def send(message):
            sent.append(message)

        await RequestBodyLimit(consume, max_bytes=6)(
            {"type": "http", "method": "POST", "headers": [], "path": "/", "raw_path": b"/", "query_string": b"", "http_version": "1.1", "scheme": "http", "server": ("test", 80), "client": ("test", 1)},
            receive,
            send,
        )
        assert sent[0]["status"] == 413

    asyncio.run(run())


def test_report_is_inert(tmp_path):
    payload = '</script><script>window.REVIEW_MARKER=1</script>'
    report = tmp_path / "report.html"
    generate_html_dashboard({"run_id": payload}, [{"file_name": payload, "html_content": '<table onclick="x()"><tr><td><img src=x onerror=alert(1)>safe</td></tr></table>'}], [], report)
    content = report.read_text()
    assert payload not in content
    assert 'onclick="x()"' not in content
    assert "onerror=" not in content
    assert sanitize_tables('<table><tr><td><script>alert(1)</script>ok</td></tr></table>') == '<table><tr><td>ok</td></tr></table>'
    assert _spreadsheet_safe("=1+1") == "'=1+1"
