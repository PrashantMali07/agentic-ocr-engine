"""Regenerate checked-in API contracts from the current FastAPI routes."""

import json
from pathlib import Path

import yaml

from ingestion_pipeline.api import app


def main() -> None:
    schema = app.openapi()
    schema["info"]["description"] = (
        "Backend-only document extraction service. Document endpoints require a bearer "
        "API key configured through INGESTION_API_KEY_SHA256_JSON. POST /extract/v1, "
        "GET /extract/v1/{job_id}, and DELETE /extract/v1/{job_id} use a durable "
        "single-host queue. GET /health is public liveness, GET /ready is public "
        "readiness, and GET /operations/metrics requires an admin role. Run the separate ingestion worker "
        "against the same SQLite database and artifact directory."
    )
    schema["servers"] = [{
        "url": "http://127.0.0.1:8000",
        "description": "Local or private-network endpoint",
    }]
    disabled = {
        "/extract": "Disabled by default; requires ENABLE_SYNC_EXTRACTION=true for controlled compatibility testing.",
        "/validate-attachments": "Disabled by default; requires ENABLE_SYNC_EXTRACTION=true for controlled compatibility testing.",
        "/extract-dir": "Disabled; server-directory extraction is unavailable over HTTP.",
        "/batch-extract": "Disabled; server-directory batch extraction is unavailable over HTTP.",
    }
    for route, note in disabled.items():
        operation = next(iter(schema["paths"][route].values()))
        operation["description"] = note
        operation["x-default-availability"] = "disabled"
    for route in ["/extract/v1", "/extract/v1/{job_id}",
                  "/extract/v1/{job_id}/review", "/extract/v1/{job_id}/reviews"]:
        for operation in schema["paths"][route].values():
            operation["x-default-availability"] = "enabled"
    schema["paths"]["/health"]["get"]["x-default-availability"] = "enabled-public"
    schema["paths"]["/ready"]["get"]["x-default-availability"] = "enabled-public"
    schema["paths"]["/operations/metrics"]["get"]["x-default-availability"] = "enabled-admin"
    folder = Path(__file__).resolve().parent.parent / "docs" / "api"
    (folder / "openapi.json").write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "openapi.yaml").write_text(yaml.safe_dump(schema, sort_keys=False, allow_unicode=True), encoding="utf-8")


if __name__ == "__main__":
    main()
