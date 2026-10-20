"""
Pydantic models for API request validation.
"""

import uuid
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractRequest(BaseModel):
    """
    Request model for document extraction from uploaded files.

    Note: Actual file upload is handled via multipart/form-data.
    This model defines the form fields.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "request_id": "auto-generated-uuid",
                "save_visualizations": False,
            }
        }
    )

    request_id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Optional request identifier for tracking (auto-generated if not provided)",
        examples=["req-abc-123", "550e8400-e29b-41d4-a716-446655440000"],
    )

    save_visualizations: bool = Field(
        default=False,
        description="Whether to save OCR visualization images",
    )


class ExtractDirRequest(BaseModel):
    """
    Request model for document extraction from a server directory path.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "request_id": "auto-generated-uuid",
                "directory_path": "/data/inbox/batch_001",
                "save_visualizations": False,
            }
        }
    )

    request_id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Optional request identifier for tracking (auto-generated if not provided)",
        examples=["req-abc-123", "550e8400-e29b-41d4-a716-446655440000"],
    )

    directory_path: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="Server-side directory path containing documents to process",
        examples=["/data/inbox/batch_001", "./attachments/batch_001"],
    )

    save_visualizations: bool = Field(
        default=False,
        description="Whether to save OCR visualization images",
    )


class ValidateAttachmentsRequest(BaseModel):
    """
    Request model for validating uploaded attachments without extraction.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "request_id": "auto-generated-uuid",
            }
        }
    )

    request_id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Optional request identifier for tracking (auto-generated if not provided)",
        examples=["req-abc-123", "550e8400-e29b-41d4-a716-446655440000"],
    )


class BatchExtractRequest(BaseModel):
    """
    Request model for batch extraction of multiple directories.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "request_id": "auto-generated-uuid",
                "directories": [
                    {"request_id": "req-001", "directory_path": "/data/inbox/batch_001"},
                    {"request_id": "req-002", "directory_path": "/data/inbox/batch_002"},
                ],
                "save_visualizations": False,
            }
        }
    )

    request_id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Optional batch request identifier",
        examples=["batch-req-abc-123"],
    )

    directories: List[ExtractDirRequest] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="List of directories to process in batch",
    )

    save_visualizations: bool = Field(
        default=False,
        description="Whether to save OCR visualization images for all directories",
    )


class ReviewCorrection(BaseModel):
    """Replace one exact span in a selected document's current content."""

    start: int = Field(..., ge=0, description="Zero-based Unicode character offset")
    end: int = Field(..., gt=0, description="Exclusive Unicode character offset")
    expected: str = Field(..., min_length=1, max_length=10_000)
    replacement: str = Field(..., max_length=10_000)

    @model_validator(mode="after")
    def valid_span(self):
        if self.end <= self.start or self.end - self.start != len(self.expected):
            raise ValueError("Correction span must match expected text length")
        if any(char in self.replacement for char in "<>&"):
            raise ValueError("Correction replacement cannot contain HTML markup or entities")
        return self


class ReviewRequest(BaseModel):
    """Approve one pending job result, optionally correcting one document."""

    document_name: str = Field(..., min_length=1, max_length=255)
    result_version: int = Field(..., ge=0)
    corrections: List[ReviewCorrection] = Field(default_factory=list, max_length=50)
