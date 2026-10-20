"""Round-trip and metadata-budget tests for the Phase 00 V1 contracts.

These tests import only ``ingestion_pipeline.contracts`` so they run without
touching GPU/OCR dependencies. The build spec requires: examples serialize
and deserialize across process boundaries, and workflow payloads stay within
the chosen metadata budget.
"""

import pytest
from pydantic import BaseModel

from ingestion_pipeline.contracts import (
    DEFAULT_METADATA_BUDGET_BYTES,
    MetadataBudgetError,
    assert_metadata_budget,
    example_completed_page_result,
    example_document_result,
    example_engine_provenance,
    example_job_request,
    example_page_manifest,
    example_review_decision,
    example_route_decision,
    example_unresolved_page_result,
    example_unsupported_page_manifest,
)

EXAMPLE_BUILDERS = [
    example_job_request,
    example_page_manifest,
    example_unsupported_page_manifest,
    example_route_decision,
    example_engine_provenance,
    example_completed_page_result,
    example_unresolved_page_result,
    lambda: example_review_decision(approve=False),
    lambda: example_review_decision(approve=True),
]

MODELS_UNDER_BUDGET = [
    example_job_request,
    example_page_manifest,
    example_route_decision,
    lambda: example_completed_page_result(),
]


def test_every_example_round_trips_through_json():
    for builder in EXAMPLE_BUILDERS:
        model = builder()
        restored = type(model).model_validate_json(model.model_dump_json())
        assert restored == model


def test_unknown_confidence_stays_explicit_none_never_coerced():
    unresolved = example_unresolved_page_result()
    assert unresolved.confidence is None
    restored = type(unresolved).model_validate_json(unresolved.model_dump_json())
    assert restored.confidence is None
    # Regression guard for the SECURITY_REVIEW defect where missing
    # confidence surfaced as a default 1.0 "success".
    assert restored.model_dump()["confidence"] is None


def test_document_result_outcomes():
    assert example_document_result("complete").outcome == "completed"
    assert example_document_result("partial").outcome == "failed"
    assert example_document_result("needs_review").outcome == "needs_review"
    assert example_document_result("failed").outcome == "failed"


def test_every_workflow_model_fits_the_metadata_budget():
    for builder in MODELS_UNDER_BUDGET:
        assert_metadata_budget(builder(), DEFAULT_METADATA_BUDGET_BYTES)


def test_advertised_document_bytes_break_the_budget():
    class BloatedMetadata(BaseModel):
        transcript: str

    with pytest.raises(MetadataBudgetError):
        assert_metadata_budget(BloatedMetadata(transcript="x" * (DEFAULT_METADATA_BUDGET_BYTES + 1)))
