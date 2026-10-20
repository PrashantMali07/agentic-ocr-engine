"""Temporal Activities for the Document Ingestion Test Lane (Phase 02)

Partitioned across two task queues:
1. 'cpu-document-tasks': planning, digital/native extraction, small audits
2. 'gpu-ocr-tasks': scanned rendering + PaddleOCR + Tesseract reconciliation.
   The GPU worker enforces max_concurrent_activities=1; a second process
   would not.

Every payload is small typed metadata: page identity, classification,
outcomes, artifact IDs and hashes. Page block payloads (raw OCR text,
tokens, tables) live in tenant/job-scoped storage, referenced by ID and
checksum. Local paths never appear in job IDs or logs and callers cannot
name arbitrary files — inputs must already exist under the job's
attachments tree at admission.
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from temporalio import activity

from ..config import DEFAULT_OUTPUT_DIR
from ..extraction import service
from ..graph.decisions import audit_page, route_page
from ..job_store import job_store
from ..processing.preflight import PreflightError
from ..storage.artifacts import ArtifactStore

log = logging.getLogger("temporal.activities")

CPU_QUEUE = "cpu-document-tasks"
GPU_QUEUE = "gpu-ocr-tasks"

STORE = ArtifactStore.from_output_root(Path(DEFAULT_OUTPUT_DIR))

METADATA_BUDGET_BYTES_LIMIT = 65_536


def _job_input_path(job_id: str, document_id: str, extension: str) -> Path:
    """Resolve the admitted attachment for a document under the job tree."""
    if not extension.startswith("."):
        raise PreflightError("bad_identifier", "Document extension must be a lowercase dotted suffix")
    return STORE.artifact_path(job_id, "attachments", document_id, extension.lower())


def _write_json_artifact(job_id: str, kind: str, key: str, payload: Any) -> dict[str, Any]:
    """Atomically write one deterministic JSON artifact and return its record."""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return STORE.put_deterministic(job_id, kind, key, encoded, suffix=".json")


def _file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _audit_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Small audit summary safe for workflow history."""
    return {
        "is_valid": bool(report.get("is_valid")),
        "overall_confidence": report.get("overall_confidence"),
        "requires_hitl": bool(report.get("requires_hitl")),
        "issue_count": int(report.get("issue_count") or len(report.get("issues") or [])),
    }


# ---------------------------------------------------------------------------
# Planning (CPU)
# ---------------------------------------------------------------------------

@activity.defn
async def plan_document_activity(job_request: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Plan every page of every admitted document (one classification pass)."""
    job_id = job_request["job_id"]
    documents = job_request["documents"]
    plan_entries: List[Dict[str, Any]] = []
    for document in documents:
        document_id = document["document_id"]
        display_name = document.get("display_name") or document_id
        extension = document.get("extension") or Path(display_name).suffix.lower()
        file_path = _job_input_path(job_id, document_id, extension)
        if file_path.is_symlink() or not file_path.is_file():
            raise FileNotFoundError(f"Admitted input for {document_id} is missing")
        source_ref = document.get("artifact")
        if source_ref:
            if source_ref.get("artifact_id") != document_id or source_ref.get("kind") != "source":
                raise ValueError("Document source reference does not match admitted identity")
            if source_ref.get("size_bytes") is not None and file_path.stat().st_size != source_ref["size_bytes"]:
                raise ValueError("Admitted source size changed")
            if source_ref.get("sha256"):
                if await asyncio.to_thread(_file_sha256, file_path) != source_ref["sha256"]:
                    raise ValueError("Admitted source checksum changed")
        doc_plan = await asyncio.to_thread(service.plan_document_pages, document_id, file_path)
        plan_entries.extend(
            {
                "document_id": document_id,
                "display_name": display_name,
                "extension": extension.lstrip("."),
                "page_number": entry.page_number,
                "total_pages": entry.total_pages,
                "classification": entry.classification,
            }
            for entry in doc_plan.entries
        )
    return plan_entries


# ---------------------------------------------------------------------------
# Per-page extraction
# ---------------------------------------------------------------------------

@activity.defn
async def route_page_activity(page: Dict[str, Any]) -> Dict[str, str]:
    return await asyncio.to_thread(route_page, page["classification"], page["extension"])


@activity.defn
async def audit_page_activity(outcome: Dict[str, Any]) -> Dict[str, str]:
    return await asyncio.to_thread(
        audit_page, outcome["status"], outcome.get("audit") or {}, outcome.get("confidence"),
    )


@activity.defn
async def confirm_review_activity(payload: Dict[str, Any]) -> bool:
    """A signal is advisory; only a committed, versioned review can resume."""
    job = await job_store.get(payload["job_id"])
    return bool(job and job.review_version == payload["result_version"] + 1
                and job.reviewed_at is not None
                and job.status.value in {"completed", "partial"})

def _page_audit_and_storage(job_id: str, outcome: service.PageOutcome) -> Dict[str, Any]:
    """Write one page's blocks to its deterministic artifact + summary."""
    entry = outcome.entry
    summary = {
        "document_id": entry.document_id,
        "display_name": entry.file_name,
        "extension": entry.file_format,
        "page_number": entry.page_number,
        "total_pages": entry.total_pages,
        "classification": entry.classification,
        "status": outcome.status,
        "error": list(outcome.error) if outcome.error else None,
    }
    if outcome.error is not None:
        summary["audit"] = {"is_valid": False, "requires_hitl": True, "issue_count": 1}
        return summary
    artifact = None
    audit_summary: Dict[str, Any] = {"is_valid": True, "requires_hitl": False, "issue_count": 0}
    if outcome.blocks:
        from ..extraction.auditor import run_document_audit

        blocks = outcome.blocks
        texts: list[str] = []
        scores: list[float] = []
        for block in blocks:
            content = block.get("content")
            if isinstance(content, str):
                texts.append(content)
            confidence = block.get("confidence")
            if isinstance(confidence, (int, float)):
                scores.append(float(confidence))
        tables_rows: list[Any] = []
        try:
            report = run_document_audit(texts=texts, scores=scores, tables_data=tables_rows, min_confidence=0.85)
            audit_summary = _audit_summary(report.summary_dict())
        except Exception as exc:
            log.warning("Page audit failed for %s p%s (%s)", entry.file_name, entry.page_number, type(exc).__name__)
            audit_summary = {"is_valid": False, "requires_hitl": True, "issue_count": 1, "audit_error": True}
        artifact = _write_json_artifact(
            job_id, "ocr", f"{entry.document_id}:{entry.page_number}:blocks", blocks,
        )
    confidences = [
        b["confidence"] for b in (outcome.blocks or [])
        if isinstance(b.get("confidence"), (int, float)) and 0.0 <= b["confidence"] <= 1.0
    ]
    summary["confidence"] = round(sum(confidences) / len(confidences), 4) if confidences else None
    summary["text_artifact"] = artifact["artifact_id"] if artifact else None
    summary["text_artifact_sha256"] = artifact["sha256"] if artifact else None
    summary["audit"] = audit_summary
    return summary


@activity.defn
async def digital_page_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract one planned digital PDF page (CPU queue)."""
    job_id = payload["job_id"]
    entry = service.PagePlanEntry(
        document_id=payload["document_id"],
        file_name=payload["display_name"],
        file_format=payload["extension"],
        page_number=payload["page_number"],
        classification="digital",
        total_pages=payload["total_pages"],
    )
    file_path = _job_input_path(job_id, payload["document_id"], f".{payload['extension']}")
    blocks = await asyncio.to_thread(service.extract_digital_pages, [entry], file_path)
    for block in blocks:
        block["source_file"] = entry.file_name
    # Terminal state: a digital page either produced text or is explicitly empty.
    outcome = service.PageOutcome(entry=entry, blocks=blocks,
                                  status="completed" if blocks else "empty")
    return await asyncio.to_thread(_page_audit_and_storage, job_id, outcome)


@activity.defn
async def scanned_page_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Render + OCR + reconcile one planned scanned page (GPU queue, concurrency 1)."""
    job_id = payload["job_id"]
    entry = service.PagePlanEntry(
        document_id=payload["document_id"],
        file_name=payload["display_name"],
        file_format=payload["extension"],
        page_number=payload["page_number"],
        classification="scanned",
        total_pages=payload["total_pages"],
    )
    file_path = _job_input_path(job_id, payload["document_id"], f".{payload['extension']}")
    work = service.PageWork(entry=entry, file_path=file_path)
    outcome = await asyncio.to_thread(service.run_pdf_page, work)
    if outcome.error is not None and outcome.status == "failed" and outcome.error[0] in {"decode_failed", "password_protected"}:
        raise PreflightError(outcome.error[0], outcome.error[1])
    return await asyncio.to_thread(_page_audit_and_storage, job_id, outcome)


@activity.defn
async def image_page_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """OCR one standalone image attachment (GPU queue, concurrency 1)."""
    job_id = payload["job_id"]
    entry = service.PagePlanEntry(
        document_id=payload["document_id"],
        file_name=payload["display_name"],
        file_format=payload["extension"],
        page_number=1,
        classification="scanned",
        total_pages=1,
    )
    file_path = _job_input_path(job_id, payload["document_id"], f".{payload['extension']}")
    work = service.PageWork(entry=entry, file_path=file_path)
    outcome = await asyncio.to_thread(service.run_image_page, work)
    return await asyncio.to_thread(_page_audit_and_storage, job_id, outcome)


@activity.defn
async def native_document_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract one native document (docx/html/xlsx/csv) as its single page (CPU)."""
    job_id = payload["job_id"]
    entry = service.PagePlanEntry(
        document_id=payload["document_id"],
        file_name=payload["display_name"],
        file_format=payload["extension"],
        page_number=1,
        classification="digital",
        total_pages=1,
    )
    file_path = _job_input_path(job_id, payload["document_id"], f".{payload['extension']}")
    outcomes = await asyncio.to_thread(
        service.run_native_document, service.DocumentPlan(entries=[entry]), file_path,
        image_out_dir=Path(DEFAULT_OUTPUT_DIR) / "jobs" / job_id / "images",
    )
    for block in outcomes[0].blocks or []:
        block["source_file"] = entry.file_name
    return await asyncio.to_thread(_page_audit_and_storage, job_id, outcomes[0])


# ---------------------------------------------------------------------------
# Webhook (fail closed; kept from the previous design)
# ---------------------------------------------------------------------------

@activity.defn
async def dispatch_webhook_activity(payload: Dict[str, Any]) -> bool:
    """Fail closed until tenant-owned delivery and egress controls are installed."""
    if payload.get("callback_url"):
        raise ValueError("Callback delivery is disabled")
    return True


# Legacy names kept out deliberately: the previous triage + separate
# Paddle/table/fallback activities move behind the shared service (Phase 01)
# and their mid-workflow payloads (texts/boxes/tables) no longer traverse
# the workflow history.
