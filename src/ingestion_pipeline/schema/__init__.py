"""
Schema package for Pydantic models used in API request/response validation.
"""

from .common import ConfidenceScore, EmailId, FileName, PageNumber
from .requests import (
    BatchExtractRequest,
    ExtractDirRequest,
    ExtractRequest,
    ReviewCorrection,
    ReviewRequest,
    ValidateAttachmentsRequest,
)
from .responses import (
    BatchExtractResponse,
    DiffResult,
    DocumentContent,
    ErrorResponse,
    ExtractResponse,
    HealthResponse,
    JobProgress,
    JobStatus,
    JobStatusResponse,
    ValidateAttachmentsResponse,
    ValidationResult,
)

__all__ = [
    # Requests
    "ExtractRequest",
    "ExtractDirRequest",
    "ValidateAttachmentsRequest",
    "BatchExtractRequest",
    "ReviewRequest",
    "ReviewCorrection",
    # Responses
    "DocumentContent",
    "ExtractResponse",
    "ValidateAttachmentsResponse",
    "ValidationResult",
    "DiffResult",
    "ErrorResponse",
    "BatchExtractResponse",
    "HealthResponse",
    # Job lifecycle (re-exports used by job store and API clients)
    "JobProgress",
    "JobStatus",
    "JobStatusResponse",
    # Common types
    "EmailId",
    "FileName",
    "ConfidenceScore",
    "PageNumber",
]
