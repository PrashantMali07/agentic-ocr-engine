import asyncio
import hashlib
import logging
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from .config import DEFAULT_OUTPUT_DIR, feature_enabled, generate_request_id
from .contracts.models import ArtifactRef, DocumentInputV1, EnginePolicyV1, JobRequestV1
from .core.safe_html import sanitize_tables
from .extraction.manager import process_email_attachments
from .job_store import QueueFull, ReviewConflict, job_store
from .model_artifacts import configured_model_directories
from .processing.preflight import PreflightError
from .processing.validation import get_diff, validate_email
from .schema import (
    BatchExtractRequest,
    BatchExtractResponse,
    DiffResult,
    ErrorResponse,
    ExtractResponse,
    HealthResponse,
    JobStatusResponse,
    ReviewRequest,
    ValidateAttachmentsResponse,
    ValidationResult,
)
from .security.auth import Principal, require_admin, require_extractor, require_principal, require_reviewer
from .security.body_limit import RequestBodyLimit
from .security.early_auth import EarlyDocumentAuth
from .storage.uploads import MAX_REQUEST_BYTES, StoredUpload, save_uploads

"""
LogisticAI Ingestion API

This module implements the FastAPI application for the PaddleOCR service.
It provides endpoints for synchronous and asynchronous document extraction,
validation, and batch processing.

Key components:
- Async job processing with semaphore-based concurrency control
- SQLite-backed job store for persistence
- Integration with PaddleOCR engine and native document parsers
- Language detection and confidence scoring
"""

# Language detection import (lightweight, CPU-only). If unavailable, fall back to no detection.
try:
    from langdetect import LangDetectException, detect
except ImportError:  # pragma: no cover
    def detect(text):
        return None
    class LangDetectException(Exception):
        pass

log = logging.getLogger("ingest.api")

@asynccontextmanager
async def _lifespan(application: FastAPI):
    configured_model_directories()  # Fail startup before accepting jobs if packaged models changed.
    reconciler = None
    if feature_enabled("ENABLE_TEMPORAL_TEST_LANE"):
        from .temporal.bridge import run_reconciler
        reconciler = asyncio.create_task(run_reconciler(), name="temporal-lane-reconciler")
    try:
        yield
    finally:
        if reconciler is not None:
            reconciler.cancel()
            await asyncio.gather(reconciler, return_exceptions=True)


app = FastAPI(title="LogisticAI Ingestion API", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)
app.add_middleware(RequestBodyLimit, max_bytes=MAX_REQUEST_BYTES + 256 * 1024)
app.add_middleware(EarlyDocumentAuth)


@app.exception_handler(PreflightError)
async def document_rejection_handler(request, exc: PreflightError):
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(error=exc.code, detail=str(exc)).model_dump(exclude_none=True),
    )



def _available(name: str) -> None:
    if not feature_enabled(name):
        raise HTTPException(status_code=503, detail="This extraction route is disabled")


def _validate_engine(engine: str) -> None:
    if engine == "legacy":
        return
    if engine in {"langgraph", "decoupled"} and feature_enabled("ENABLE_LANGGRAPH_ENGINE"):
        return
    raise HTTPException(status_code=400, detail="Extraction engine is unavailable")


def _display_documents(result: dict, uploads: list[StoredUpload]) -> dict:
    names = {item.path.name: item.original_name for item in uploads}
    result["documents"] = {names.get(key, key): value for key, value in result["documents"].items()}
    return result


def _safe_graph_html(result: dict) -> dict:
    result["html_content"] = sanitize_tables(result.get("html_content", ""))
    for table in result.get("tables", []):
        if isinstance(table, dict) and "html" in table:
            table["html"] = sanitize_tables(table["html"])
    return result


def _sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()

def transform_to_document_map(record: dict) -> dict:
    """Transforms the pipeline record into the requested JSON structure, adding language detection.

    Output format:
    {
        "request_id": "...",
        "documents": {
            "filename": {
                "content": "...",
                "avg_confidence": 0.92,
                "format": "pdf",
                "pages": 3,
                "language": "en"
            },
            ...
        }
    }
    """
    blocks = record.get("blocks", [])
    attachments = record.get("attachments", [])

    doc_map = {}
    for att in attachments:
        fname = att["file"]
        # Filter blocks for this specific document and preserve order
        file_blocks = sorted(
            [b for b in blocks if b["source_file"] == fname],
            key=lambda b: (b.get("page") or 0, b["order"]),
        )

        # Join content of all blocks
        content = "\n\n".join([str(b.get("content", "")) for b in file_blocks])

        # Language detection - fall back to None if detection fails or text is too short
        try:
            language = detect(content) if content.strip() else None
        except LangDetectException:
            language = None

        doc_map[fname] = {
            "content": content,
            "avg_confidence": att.get("avg_confidence"),
            "format": att.get("format"),
            "pages": att.get("pages"),
            "language": language,
            "is_password_required": att.get("is_password_required", False),
            "status": "partial" if att.get("empty_pages") or att.get("failed_pages") else "complete",
            "processed_pages": att.get("processed_pages"),
            "empty_pages": att.get("empty_pages"),
            "failed_pages": att.get("failed_pages"),
            "correction_suggestions": [
                {"page": b.get("page"), "order": b.get("order"),
                 "original": b.get("content"), "proposed": b["suggested_content"]}
                for b in file_blocks if "suggested_content" in b
            ] or None,
        }

    return {
        "request_id": record.get("email_id"),  # email_id is used internally as request_id
        "documents": doc_map,
    }

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    schema["openapi"] = "3.0.3"  # downgrade declared version

    # Patch file upload fields: contentMediaType -> format: binary
    def fix_binary(node):
        if isinstance(node, dict):
            if node.get("contentMediaType"):
                node["format"] = "binary"
                node.pop("contentMediaType", None)
            for v in node.values():
                fix_binary(v)
        elif isinstance(node, list):
            for v in node:
                fix_binary(v)

    fix_binary(schema.get("components", {}).get("schemas", {}))
    app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi

# Endpoints

@app.post(
    "/extract",
    response_model=ExtractResponse,
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
        400: {"model": ErrorResponse, "description": "Bad request"},
        409: {"model": ErrorResponse, "description": "Graph result requires human review"},
    },
    summary="Extract text from uploaded documents",
    description="Upload multiple documents and return the extracted text grouped by filename.",
)
async def extract_documents(
    request_id: str = Form(default_factory=lambda: str(uuid.uuid4()), description="Optional request identifier for tracking (auto-generated if not provided)"),
    files: List[UploadFile] = File(..., description="Upload one or more document files to extract text from."),
    save_visualizations: bool = Form(False, description="Whether to save OCR visualization images"),
    engine: str = Form("legacy", description="Extraction engine"),
    principal: Principal = Depends(require_extractor),
) -> ExtractResponse:
    """Receive uploaded files, run extraction pipeline, and return results with language detection."""
    _available("ENABLE_SYNC_EXTRACTION")
    _validate_engine(engine)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        attachments_dir = tmp_path / "attachments"
        output_dir = tmp_path / "output"
        attachments_dir.mkdir()

        uploads = await save_uploads(files, attachments_dir)
        file_paths = [str(item.path) for item in uploads]

        log.info("Received %d files (engine=%s)", len(files), engine)

        if engine in ("langgraph", "decoupled"):
            from .graph.workflow import ReviewRequired, run_document_graph

            documents_map = {}
            for item in uploads:
                p_str = str(item.path)
                p = Path(p_str)
                try:
                    graph_result = await asyncio.to_thread(run_document_graph, p, thread_id=item.document_id)
                except ReviewRequired as exc:
                    raise HTTPException(
                        status_code=409,
                        detail="Human review required; submit through the durable extraction route",
                    ) from exc
                res = _safe_graph_html(graph_result)
                clean_text = res.get("clean_text", "")
                lang = detect(clean_text) if clean_text else None
                audit = res.get("audit", {})

                documents_map[p.name] = {
                    "content": clean_text,
                    "html_content": res.get("html_content", ""),
                    "tables": res.get("tables", []),
                    "avg_confidence": audit.get("overall_confidence"),
                    "format": res.get("format", p.suffix.lstrip(".")),
                    "pages": res.get("pages", 1),
                    "language": lang,
                    "audit": audit,
                    "engine_used": res.get("engine_used", "langgraph"),
                }
            result = {"request_id": request_id, "documents": documents_map}
        else:
            record = await asyncio.to_thread(process_email_attachments,
                file_paths=[Path(p) for p in file_paths],
                output_dir=str(output_dir),
                email_id=request_id,
                save_visualizations=save_visualizations,
            )
            result = transform_to_document_map(record)
            result["request_id"] = request_id

        return ExtractResponse(**_display_documents(result, uploads))

@app.post(
    "/extract/v1",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobStatusResponse,
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
        400: {"model": ErrorResponse, "description": "Bad request"},
    },
    summary="Submit documents for extraction",
    description="Upload one or more documents and enqueue an extraction job. "
                "Poll GET /extract/v1/{job_id} for status and, once completed, the result. "
                "Callbacks and visualizations are currently unavailable.",
)
async def submit_extraction_job(
    files: List[UploadFile] = File(..., description="Upload one or more document files to extract text from."),
    save_visualizations: bool = Form(False, description="Must be false; visualizations are unavailable for durable jobs"),
    callback_url: Optional[str] = Form(None, description="Must be omitted; callbacks are unavailable"),
    engine: str = Form("legacy", description="Extraction engine"),
    principal: Principal = Depends(require_extractor),
) -> JobStatusResponse:
    """Save uploaded files under a job-scoped directory and enqueue extraction."""
    temporal_lane = engine == "temporal_test" and feature_enabled("ENABLE_TEMPORAL_TEST_LANE")
    if engine != "legacy" and not temporal_lane:
        raise HTTPException(status_code=400, detail="Extraction engine is unavailable for durable jobs")
    if temporal_lane and "admin" not in principal.roles:
        raise HTTPException(status_code=403, detail="Temporal test lane access is required")
    if callback_url:
        raise HTTPException(status_code=400, detail="Callbacks are not enabled")
    if save_visualizations:
        raise HTTPException(status_code=400, detail="OCR visualizations are unavailable for durable jobs")
    job_id = uuid.uuid4().hex if temporal_lane else generate_request_id()
    attachments_dir = Path(DEFAULT_OUTPUT_DIR) / "jobs" / job_id / "attachments"
    try:
        uploads = await save_uploads(files, attachments_dir)
        manifest = [
            {"document_id": item.document_id, "original_name": item.original_name,
             "size": item.size, "save_visualizations": save_visualizations}
            for item in uploads
        ]
        lane_request = None
        if temporal_lane:
            now = datetime.now(timezone.utc)
            documents = []
            for item in uploads:
                digest = await asyncio.to_thread(_sha256_file, item.path)
                documents.append(DocumentInputV1(
                    document_id=item.document_id,
                    artifact=ArtifactRef(artifact_id=item.document_id, kind="source", sha256=digest, size_bytes=item.size),
                    display_name=item.original_name,
                ))
            lane_request = JobRequestV1(
                job_id=job_id, tenant_id=principal.tenant_id, owner_id=principal.subject,
                policy_version="v1", engine_policy=EnginePolicyV1(), documents=documents,
                submitted_at=now, deadline=now + timedelta(minutes=20),
            ).model_dump(mode="json")
        job = await job_store.admit(
            job_id, principal, manifest, sum(item.size for item in uploads),
            scheduler_kind="temporal_test" if temporal_lane else "legacy", lane_request=lane_request,
        )
    except QueueFull as exc:
        shutil.rmtree(attachments_dir.parent, ignore_errors=True)
        raise HTTPException(status_code=429, detail="Extraction queue is full") from exc
    except BaseException:
        shutil.rmtree(attachments_dir.parent, ignore_errors=True)
        raise
    log.info("Admitted durable job %s with %d files", job_id, len(files))

    return job


@app.get(
    "/extract/v1/{job_id}",
    response_model=JobStatusResponse,
    responses={404: {"model": ErrorResponse, "description": "Job not found"},
               409: {"model": ErrorResponse, "description": "Stale or invalid review"}},
    summary="Get extraction job status/result",
    description="Poll for a submitted job's status. Once status is 'completed', "
                "the `result` field holds the same payload the old synchronous /extract returned.",
)
async def get_extraction_job(job_id: str, principal: Principal = Depends(require_principal)) -> JobStatusResponse:
    job = await job_store.get_for_principal(job_id, principal)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found or expired")
    return job


@app.delete(
    "/extract/v1/{job_id}",
    response_model=JobStatusResponse,
    summary="Cancel an extraction job",
)
async def cancel_extraction_job(job_id: str, principal: Principal = Depends(require_extractor)) -> JobStatusResponse:
    job = await job_store.get_for_principal(job_id, principal)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.owner_id != principal.subject:
        raise HTTPException(status_code=403, detail="Only the job owner can cancel it")
    if not await job_store.cancel(job_id, principal):
        raise HTTPException(status_code=409, detail="Job is already terminal")
    return await job_store.get_for_principal(job_id, principal)


@app.post(
    "/extract/v1/{job_id}/review",
    response_model=JobStatusResponse,
    responses={404: {"model": ErrorResponse, "description": "Job not found"}},
    summary="Submit human review corrections for an extraction job",
    description="Provides operator overrides/corrections for a job flagged by the Auditor, resolving HITL status.",
)
async def review_extraction_job(
    job_id: str,
    review: ReviewRequest,
    principal: Principal = Depends(require_reviewer),
) -> JobStatusResponse:
    try:
        job = await job_store.submit_review(job_id, principal, review)
    except ReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return job


@app.get(
    "/extract/v1/{job_id}/reviews",
    summary="List accepted reviews for an extraction job",
)
async def get_extraction_reviews(
    job_id: str, principal: Principal = Depends(require_reviewer),
) -> List[Dict[str, Any]]:
    events = await job_store.review_history(job_id, principal)
    if events is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return events

@app.post(
    "/extract-dir",
    response_model=ExtractResponse,
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
        400: {"model": ErrorResponse, "description": "Bad request - no supported files found"},
    },
    summary="Extract text from documents in a server directory",
    description="Process documents from a local directory path on the server.",
)
async def extract_directory(
    request_id: str = Form(default_factory=lambda: str(uuid.uuid4()), description="Optional request identifier for tracking (auto-generated if not provided)"),
    directory_path: str = Form(..., description="Server-side directory path containing documents to process"),
    save_visualizations: bool = Form(False, description="Whether to save OCR visualization images"),
    principal: Principal = Depends(require_extractor),
) -> ExtractResponse:
    """Collect files from a directory and run extraction (language detection included)."""
    raise HTTPException(status_code=503, detail="Server directory access requires tenant-scoped storage")

@app.post(
    "/validate-attachments",
    response_model=ValidateAttachmentsResponse,
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
    summary="Validate uploaded attachments",
    description="Validate uploaded document files by extracting text and comparing against originals. Optionally include diff output.",
)
async def validate_attachments(
    request_id: str = Form(default_factory=lambda: str(uuid.uuid4()), description="Optional request identifier for tracking (auto-generated if not provided)"),
    files: List[UploadFile] = File(..., description="Upload one or more document files to validate."),
    save_visualizations: bool = Form(False, description="Whether to save OCR visualization images"),
    include_diff: bool = Form(False, description="Include git-style diff between original and extracted text"),
    principal: Principal = Depends(require_extractor),
) -> ValidateAttachmentsResponse:
    """Validate uploaded files and return per-attachment validation results (language detection is not needed here)."""
    _available("ENABLE_SYNC_EXTRACTION")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        attachments_dir = tmp_path / "attachments"
        output_dir = tmp_path / "output"
        attachments_dir.mkdir()

        # Save uploaded files
        uploads = await save_uploads(files, attachments_dir)
        file_paths = [str(item.path) for item in uploads]

        log.info("Received %d files", len(files))

        # Run extraction pipeline
        record = await asyncio.to_thread(process_email_attachments,
            file_paths=[Path(p) for p in file_paths],
            output_dir=str(output_dir),
            email_id=request_id,
            save_visualizations=save_visualizations,
        )

        # Run validation
        validation_result = await asyncio.to_thread(validate_email, record, str(attachments_dir))
        original_names = {item.path.name: item.original_name for item in uploads}

        # Build validation response list
        results = []
        valid_count = 0
        invalid_count = 0
        for att in record["attachments"]:
            fname = att["file"]
            report = validation_result["per_attachment"].get(fname, {})
            flags = report.get("flags", [])
            is_valid = len(flags) == 0
            diff_obj = None
            if include_diff:
                diff_data = get_diff(record, str(attachments_dir), fname)
                if diff_data:
                    diff_obj = DiffResult(**diff_data)
            result = ValidationResult(
                filename=original_names.get(fname, fname),
                valid=is_valid,
                format=att.get("format"),
                size_bytes=(Path(str(attachments_dir / fname)).stat().st_size if (Path(str(attachments_dir / fname)).exists()) else None),
                pages=att.get("pages"),
                errors=flags if flags else None,
                diff=diff_obj,
            )
            results.append(result)
            if is_valid:
                valid_count += 1
            else:
                invalid_count += 1

        return ValidateAttachmentsResponse(
            request_id=request_id,
            results=results,
            total_files=len(files),
            valid_count=valid_count,
            invalid_count=invalid_count,
        )

def get_all_endpoints():
    data = {}
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            if route.path in ["/docs", "/redoc", "/openapi.json"]:
                continue
            data[route.name] = {
                "path": route.path,
                "methods": list(route.methods)
            }
    return data

@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="healthy",
        version=app.version,
        allowed_file_extension=set(),
        endpoints={},
        description="Multi-extension data extraction pipeline using PaddleOCR."
    )


@app.get("/ready", summary="Check local job service readiness")
async def readiness() -> Dict[str, str]:
    try:
        snapshot = await job_store.operational_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Job service unavailable") from exc
    age = snapshot["worker_last_seen_seconds"]
    if age is None or age > 10 or snapshot["disk_free_bytes"] < 256 * 1024 * 1024:
        raise HTTPException(status_code=503, detail="Job service unavailable")
    return {"status": "ready"}


@app.get("/operations/metrics", summary="Read local queue and retention metrics")
async def operational_metrics(principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return await job_store.operational_snapshot()

@app.post(
    "/batch-extract",
    response_model=BatchExtractResponse,
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
        400: {"model": ErrorResponse, "description": "Bad request"},
    },
    summary="Batch extract documents from multiple directories",
    description="Process multiple document directories in a single request.",
)
async def batch_extract(
    request: BatchExtractRequest,
    principal: Principal = Depends(require_extractor),
) -> BatchExtractResponse:
    """Batch extract documents from multiple directories, each returning language info."""
    raise HTTPException(status_code=503, detail="Server directory access requires tenant-scoped storage")


def main():
    from .config import OCR_DPI, OCR_DTYPE, OCR_ENHANCE, OCR_GRAYSCALE, OCR_MAX_DIM
    print("Starting Ingestion API with OCR settings: DPI={OCR_DPI}, ENHANCE={OCR_ENHANCE}, MAX_DIM={OCR_MAX_DIM}, DTYPE={OCR_DTYPE}, GRAYSCALE={OCR_GRAYSCALE}".format(
        OCR_DPI=OCR_DPI,
        OCR_ENHANCE=OCR_ENHANCE,
        OCR_MAX_DIM=OCR_MAX_DIM,
        OCR_DTYPE=OCR_DTYPE,
        OCR_GRAYSCALE=OCR_GRAYSCALE
    ))
    print(get_all_endpoints())
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()
