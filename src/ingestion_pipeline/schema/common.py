"""
Common type definitions and reusable Pydantic field types.
"""

from typing import Annotated

from pydantic import BeforeValidator, Field
from typing_extensions import TypedDict

# Type aliases with validation
EmailId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="Unique identifier for the email/message",
        examples=["email_123", "msg-abc-456"],
    ),
]

FileName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        description="Name of the uploaded file",
        examples=["document.pdf", "invoice_2024.docx"],
    ),
]

ConfidenceScore = Annotated[
    float,
    Field(
        ge=0.0,
        le=1.0,
        description="Confidence score between 0 and 1",
        examples=[0.95, 0.87],
    ),
]

PageNumber = Annotated[
    int,
    Field(
        ge=1,
        description="Page number (1-indexed)",
        examples=[1, 5, 42],
    ),
]

# Reusable validators
def validate_non_empty_string(value: str) -> str:
    """Ensure string is not empty or whitespace-only."""
    if not value or not value.strip():
        raise ValueError("String cannot be empty or whitespace only")
    return value.strip()

NonEmptyString = Annotated[str, BeforeValidator(validate_non_empty_string)]


class HealthCheckResponse(TypedDict):
    """Health check response structure."""
    status: str
    version: str
