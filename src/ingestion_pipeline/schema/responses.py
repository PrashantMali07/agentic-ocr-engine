"""
Pydantic models for API response validation and documentation.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..extraction.manager import ALL_SUPPORTED_EXTS
from .common import ConfidenceScore, PageNumber


class DocumentContent(BaseModel):
    """
    Content extracted from a single document.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "content": "Invoice #12345\nDate: 2024-01-15\nTotal: $1,234.56\n...",
                "avg_confidence": 0.92,
                "format": "pdf",
                "pages": 3,
                "language": "en",
            }
        }
    )

    content: str = Field(
        ...,
        description="Extracted text content from the document",
        examples=["Invoice #12345\nDate: 2024-01-15\nTotal: $1,234.56"],
    )

    avg_confidence: Optional[ConfidenceScore] = Field(
        default=None,
        description="Average OCR confidence score across all pages",
        examples=[0.92, 0.87],
    )

    format: Optional[str] = Field(
        default=None,
        description="Document format (pdf, docx, image, etc.)",
        examples=["pdf", "docx", "png", "jpg"],
    )

    pages: Optional[PageNumber] = Field(
        default=None,
        description="Number of pages in the document",
        examples=[1, 3, 10],
    )

    language: Optional[str] = Field(
        default=None,
        description="Detected language code (ISO 639‑1) of the extracted text",
        examples=["en", "fr", "de", "es"],
    )

    is_password_required: Optional[bool] = Field(
        default=False,
        description="Whether the source file appears to be password-protected and could not be read",
        examples=[False, True],
    )

    html_content: Optional[str] = Field(
        default=None,
        description="Reconstructed HTML tables extracted from the document",
    )

    tables: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Structured table objects with headers, rows, and confidence",
    )

    audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditor quality report (confidence, math check, HITL status)",
    )

    engine_used: Optional[str] = Field(
        default=None,
        description="Extraction engine used (langgraph, digital_cpu, paddle_layoutreader, legacy)",
    )

    correction_suggestions: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Proposed OCR text corrections; original recognized content remains unchanged",
    )

    status: Literal["complete", "partial"] = Field(default="complete", description="partial when some pages produced no text")
    processed_pages: Optional[List[PageNumber]] = Field(default=None, description="One-based page numbers attempted")
    empty_pages: Optional[List[PageNumber]] = Field(default=None, description="Attempted pages without extracted blocks")
    failed_pages: Optional[List[PageNumber]] = Field(default=None, description="One-based pages whose extraction failed")


class ExtractResponse(BaseModel):
    """
    Response model for document extraction endpoint.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "request_id": "550e8400-e29b-41d4-a716-446655440000",
                "documents": {
                    "invoice.pdf": {
                        "content": "Invoice #12345\nDate: 2024-01-15\nTotal: $1,234.56",
                        "avg_confidence": 0.92,
                        "format": "pdf",
                        "pages": 3,
                        "language": "en",
                    },
                    "receipt.png": {
                        "content": "Store: ABC Mart\nTotal: $45.67",
                        "avg_confidence": 0.88,
                        "format": "png",
                        "pages": 1,
                        "language": "en",
                    },
                },
            }
        }
    )

    request_id: str = Field(
        ...,
        description="Request identifier for tracking",
        examples=["550e8400-e29b-41d4-a716-446655440000", "req-abc-123"],
    )

    documents: Dict[str, DocumentContent] = Field(
        ...,
        description="Map of filename to extracted document content",
    )


class DiffResult(BaseModel):
    """
    Diff result showing differences between original and extracted text.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "filename": "invoice.pdf",
                "similarity": 0.92,
                "coverage": 0.95,
                "diff": "@@ -1,3 +1,3 @@\n Original line 1\n-Original line 2\n+Extracted line 2\n Original line 3",
            }
        }
    )

    filename: str = Field(
        ...,
        description="Name of the file (with page info for PDFs)",
        examples=["invoice.pdf", "invoice.pdf (page 2)"],
    )

    similarity: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Text similarity score (0-1)",
        examples=[0.92, 0.87],
    )

    coverage: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Word coverage score (0-1)",
        examples=[0.95, 0.88],
    )

    diff: str = Field(
        ...,
        description="Unified diff between original and extracted text",
        examples=["@@ -1,3 +1,3 @@\n Original line 1\n-Original line 2\n+Extracted line 2\n Original line 3"],
    )


class ValidationResult(BaseModel):
    """
    Validation result for a single attachment.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "filename": "document.pdf",
                "valid": True,
                "format": "pdf",
                "size_bytes": 1024000,
                "pages": 5,
                "errors": None,
                "diff": {
                    "filename": "document.pdf",
                    "similarity": 0.92,
                    "coverage": 0.95,
                    "diff": "@@ -1,3 +1,3 @@\n Original line 1\n-Original line 2\n+Extracted line 2\n Original line 3",
                },
            }
        }
    )

    filename: str = Field(
        ...,
        description="Name of the validated file",
        examples=["document.pdf", "image.png"],
    )

    valid: bool = Field(
        ...,
        description="Whether the file is valid and processable",
    )

    format: Optional[str] = Field(
        default=None,
        description="Detected file format",
        examples=["pdf", "docx", "png", "jpg"],
    )

    size_bytes: Optional[int] = Field(
        default=None,
        ge=0,
        description="File size in bytes",
        examples=[1024000, 512000],
    )

    pages: Optional[PageNumber] = Field(
        default=None,
        description="Number of pages (for multi-page documents)",
        examples=[1, 5, 10],
    )

    errors: Optional[List[str]] = Field(
        default=None,
        description="List of validation errors if invalid",
        examples=[["Corrupted PDF", "Password protected"]],
    )

    diff: Optional[DiffResult] = Field(
        default=None,
        description="Diff between original and extracted text (if available)",
    )


class ValidateAttachmentsResponse(BaseModel):
    """
    Response model for attachment validation endpoint.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "request_id": "550e8400-e29b-41d4-a716-446655440000",
                "results": [
                    {
                        "filename": "invoice.pdf",
                        "valid": True,
                        "format": "pdf",
                        "size_bytes": 1024000,
                        "pages": 5,
                        "errors": None,
                    },
                    {
                        "filename": "corrupted.docx",
                        "valid": False,
                        "format": "docx",
                        "size_bytes": 512000,
                        "pages": None,
                        "errors": ["Corrupted file", "Cannot parse"],
                    },
                ],
                "total_files": 2,
                "valid_count": 1,
                "invalid_count": 1,
            }
        }
    )

    request_id: str = Field(
        ...,
        description="Request identifier for tracking",
        examples=["550e8400-e29b-41d4-a716-446655440000", "req-abc-123"],
    )

    results: List[ValidationResult] = Field(
        ...,
        description="Validation results for each attachment",
    )

    total_files: int = Field(
        ...,
        ge=0,
        description="Total number of files validated",
        examples=[2, 10],
    )

    valid_count: int = Field(
        ...,
        ge=0,
        description="Number of valid files",
        examples=[1, 8],
    )

    invalid_count: int = Field(
        ...,
        ge=0,
        description="Number of invalid files",
        examples=[1, 2],
    )


class ErrorResponse(BaseModel):
    """
    Standard error response model.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "Internal Server Error",
                "detail": "Failed to process document: OCR engine timeout",
                "request_id": "550e8400-e29b-41d4-a716-446655440000",
            }
        }
    )

    error: str = Field(
        ...,
        description="Error type/category",
        examples=["Bad Request", "Internal Server Error", "Not Found"],
    )

    detail: str = Field(
        ...,
        description="Human-readable error description",
        examples=["Invalid file format", "Processing timeout", "Email not found"],
    )

    request_id: Optional[str] = Field(
        default=None,
        description="Associated request ID if applicable",
        examples=["550e8400-e29b-41d4-a716-446655440000", "req-abc-123"],
    )


class BatchExtractResponse(BaseModel):
    """
    Response model for batch extraction endpoint.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch_id": "batch-550e8400-e29b-41d4-a716-446655440000",
                "results": [
                    {
                        "request_id": "req-001",
                        "documents": {
                            "doc1.pdf": {
                                "content": "Content...",
                                "avg_confidence": 0.92,
                                "format": "pdf",
                                "pages": 3,
                                "language": "en",
                            }
                        },
                        "status": "success",
                        "error": None,
                    },
                    {
                        "request_id": "req-002",
                        "documents": {},
                        "status": "error",
                        "error": "Directory not found",
                    },
                ],
                "total_requests": 2,
                "successful": 1,
                "failed": 1,
            }
        }
    )

    batch_id: str = Field(
        ...,
        description="Batch request identifier",
        examples=["batch-550e8400-e29b-41d4-a716-446655440000"],
    )

    results: List[Dict[str, Any]] = Field(
        ...,
        description="Extraction results for each request",
    )

    total_requests: int = Field(
        ...,
        ge=0,
        description="Total number of requests processed",
    )

    successful: int = Field(
        ...,
        ge=0,
        description="Number of successfully processed requests",
    )

    failed: int = Field(
        ...,
        ge=0,
        description="Number of failed requests",
    )

class HealthResponse(BaseModel):
    """
    Health check response model.
    """
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "healthy",
                "version": "0.1.0",
                "allowed_file_extension": [".pdf", ".jpg", ".png"],
                "endpoints": {"/health": {"methods": ["GET"]}},
                "description": "Multi-extension data extraction pipeline using PaddleOCR.",
            }
        }
    )

    status: str = Field(..., description="Service health status")
    version: str = Field(..., description="API version")

    allowed_file_extension: set[str] = Field(
        default=ALL_SUPPORTED_EXTS,
        description="Supported File Extension."
    )

    endpoints: dict = Field(
        default_factory=dict,
        description="Application Endpoints"
    )

    description: str = Field(
       ..., description="Multi-extension Ingestion Pipeline using PaddleOCR."
    )


class JobStatus(str, Enum):
    """Lifecycle states for an async extraction job."""
    queued = "queued"
    processing = "processing"
    waiting_for_review = "waiting_for_review"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


class JobProgress(BaseModel):
    """Optional per-file progress reported while a job is processing."""
    total: int = Field(..., ge=0, description="Total number of files in the job")
    processed: int = Field(..., ge=0, description="Number of files processed so far")


class JobStatusResponse(BaseModel):
    """
    Response model for job submission and job status polling.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "20260923143012004-a1b2c3d4",
                "status": "processing",
                "created_at": "2026-09-23T14:30:12.004Z",
                "updated_at": "2026-09-23T14:30:14.512Z",
                "progress": {"total": 5, "processed": 2},
                "result": None,
                "error": None,
            }
        }
    )

    job_id: str = Field(
        ...,
        description="Sortable job identifier (also usable as the internal request_id)",
        examples=["20260923143012004-a1b2c3d4"],
    )

    # Null marks a pre-migration record. Such records are not served through
    # tenant-scoped API reads until explicitly assigned by an administrator.
    tenant_id: Optional[str] = None
    owner_id: Optional[str] = None

    status: JobStatus = Field(..., description="Current job lifecycle state")

    review_version: int = Field(default=0, ge=0, description="Optimistic version for durable review submissions")
    reviewed_at: Optional[datetime] = Field(default=None, description="UTC time of the accepted review")
    reviewed_by: Optional[str] = Field(default=None, description="Subject that accepted the review")

    created_at: datetime = Field(..., description="UTC time the job was created")

    updated_at: datetime = Field(..., description="UTC time the job was last updated")

    progress: Optional[JobProgress] = Field(
        default=None,
        description="Per-file progress while the job is processing",
    )

    result: Optional[ExtractResponse] = Field(
        default=None,
        description="Extraction result, provisional while waiting_for_review and final after approval",
    )

    error: Optional[str] = Field(
        default=None,
        description="Error message, present once status is 'failed'",
    )
