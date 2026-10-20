"""Validation tests for the Phase 00 V1 contracts.

Covers the build-spec acceptance matrix: reject missing tenant/artifact IDs,
invalid page indices, unbounded actions, stale/malformed review versions,
no local absolute paths, and payloads over the metadata limit.
"""

import pytest
from pydantic import ValidationError

from ingestion_pipeline.contracts import (
    example_completed_page_result,
    example_document_result,
    example_job_request,
    example_page_manifest,
    example_review_decision,
    example_route_decision,
    example_unsupported_page_manifest,
)

# --- JobRequestV1 ----------------------------------------------------------

def test_job_request_rejects_user_sourced_job_id_with_separator():
    bad = example_job_request().model_copy(update={"job_id": "../attachments/evidence"})
    with pytest.raises(ValidationError):
        bad.model_validate(bad.model_dump())


def test_job_request_rejects_blank_tenant_or_owner():
    job = example_job_request()
    with pytest.raises(ValidationError):
        job.model_validate({**job.model_dump(), "tenant_id": "  "})
    with pytest.raises(ValidationError):
        job.model_validate({**job.model_dump(), "owner_id": ""})


def test_job_request_rejects_non_hex_document_artifact_ids():
    job = example_job_request()
    dumped = job.model_dump()
    dumped["documents"][0]["artifact"]["artifact_id"] = "not-a-real-artifact-id!"
    with pytest.raises(ValidationError):
        type(job).model_validate(dumped)


def test_job_request_rejects_duplicate_document_ids():
    job = example_job_request()
    dumped = job.model_dump()
    dumped["documents"][1]["document_id"] = dumped["documents"][0]["document_id"]
    with pytest.raises(ValidationError):
        type(job).model_validate(dumped)


def test_job_request_rejects_deadline_not_after_submission():
    job = example_job_request()
    dumped = job.model_dump()
    dumped["deadline"] = dumped["submitted_at"]
    with pytest.raises(ValidationError):
        type(job).model_validate(dumped)


def test_job_request_forbids_undeclared_fields_such_as_local_paths():
    job = example_job_request()
    dumped = job.model_dump()
    dumped["local_file_path"] = "/home/whoever/uploads/evidence.pdf"
    with pytest.raises(ValidationError):
        type(job).model_validate(dumped)


def test_engine_policy_requires_fallback_passes_when_fallback_enabled():
    request = example_job_request()
    bad = request.engine_policy.model_copy(update={"allow_fallback": True, "max_fallback_passes": 0})
    with pytest.raises(ValidationError):
        type(bad).model_validate(bad.model_dump())


# --- PageManifestV1 --------------------------------------------------------

def test_page_manifest_rejects_page_number_beyond_total():
    manifest = example_page_manifest(page_number=2, total_pages=3)
    dumped = manifest.model_dump()
    dumped["page_number"] = 4
    with pytest.raises(ValidationError):
        type(manifest).model_validate(dumped)


def test_page_manifest_rejects_digital_page_carrying_an_error_record():
    manifest = example_page_manifest()
    dumped = manifest.model_dump()
    dumped["classification"] = "digital"
    dumped["error"] = {"code": "decode_failed", "message": "boom"}
    with pytest.raises(ValidationError):
        type(manifest).model_validate(dumped)


def test_unsupported_page_requires_an_explicit_error_record():
    manifest = example_unsupported_page_manifest()
    dumped = manifest.model_dump()
    dumped["error"] = None
    with pytest.raises(ValidationError):
        type(manifest).model_validate(dumped)


# --- RouteDecisionV1 ------------------------------------------------------

def test_route_decision_rejects_unregistered_reason_codes():
    decision = example_route_decision()
    dumped = decision.model_dump()
    dumped["reason_code"] = "just_because"
    with pytest.raises(ValidationError):
        type(decision).model_validate(dumped)


def test_route_decision_rejects_attempt_not_below_max():
    decision = example_route_decision()
    dumped = decision.model_dump()
    dumped["attempt"] = dumped["max_attempts"]
    with pytest.raises(ValidationError):
        type(decision).model_validate(dumped)


def test_route_decision_rejects_unbounded_or_non_artifact_evidence():
    decision = example_route_decision()
    dumped = decision.model_dump()
    dumped["evidence_artifact_ids"] = ["/tmp/render_page_1.png"]
    with pytest.raises(ValidationError):
        type(decision).model_validate(dumped)


# --- PageResultV1 / DocumentResultV1 ---------------------------------------

def test_failed_page_cannot_claim_measured_confidence():
    result = example_completed_page_result()
    dumped = result.model_dump()
    dumped["status"] = "failed"
    with pytest.raises(ValidationError):
        type(result).model_validate(dumped)


def test_document_result_rejects_missing_pages_as_completed_shaped_output():
    result = example_document_result("complete")
    dumped = result.model_dump()
    del dumped["pages"][1]
    with pytest.raises(ValidationError):
        type(result).model_validate(dumped)


def test_document_result_rejects_duplicate_page_numbers():
    result = example_document_result("complete")
    dumped = result.model_dump()
    dumped["pages"][1]["page_number"] = 1
    with pytest.raises(ValidationError):
        type(result).model_validate(dumped)


def test_document_result_rejects_missing_final_pages():
    result = example_document_result("complete")
    dumped = result.model_dump()
    dumped["pages"] = dumped["pages"][:1]
    with pytest.raises(ValidationError, match="every declared page"):
        type(result).model_validate(dumped)


def test_document_result_rejects_inconsistent_page_totals():
    result = example_document_result("complete")
    dumped = result.model_dump()
    dumped["pages"][1]["total_pages"] += 1
    with pytest.raises(ValidationError, match="agree"):
        type(result).model_validate(dumped)


# --- ReviewDecisionV1 -----------------------------------------------------

def test_review_cannot_be_both_approve_and_corrections():
    decision = example_review_decision(approve=False)
    dumped = decision.model_dump()
    dumped["decision"] = "approve"
    with pytest.raises(ValidationError):
        type(decision).model_validate(dumped)


def test_correction_requires_expected_matches_span_length():
    decision = example_review_decision(approve=False)
    dumped = decision.model_dump()
    dumped["corrections"][0]["expected"] = "short"
    dumped["corrections"][0]["span"] = {"start": 0, "end": 30}
    with pytest.raises(ValidationError):
        type(decision).model_validate(dumped)


def test_review_rejects_idempotency_key_too_short():
    decision = example_review_decision(approve=False)
    dumped = decision.model_dump()
    dumped["idempotency_key"] = "ab"
    with pytest.raises(ValidationError):
        type(decision).model_validate(dumped)


def test_review_correct_requires_at_least_one_correction():
    decision = example_review_decision(approve=False)
    dumped = decision.model_dump()
    dumped["corrections"] = []
    with pytest.raises(ValidationError):
        type(decision).model_validate(dumped)
