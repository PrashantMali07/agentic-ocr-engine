"""Canonical serialization examples for V1 contracts.

These examples are the executable contract fixtures referenced by the build
spec and consumed by later phases (Phase 01 differential baselines, Phase 02
workflow payloads, Phase 03 decision traces). They cover: complete, partial,
needs-review, failure, retry, and cancellation shapes.

Every builder is deterministic (fixed hashes/IDs/timestamps), so diff output
is stable and JSON fixtures can be pinned in tests or handoff evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import (
    ArtifactRef,
    ConfidenceValue,
    CorrectionV1,
    DocumentInputV1,
    DocumentResultV1,
    EnginePolicyV1,
    EngineProvenanceV1,
    FieldProvenanceV1,
    JobRequestV1,
    PageErrorV1,
    PageManifestV1,
    PageResultV1,
    ReviewDecisionV1,
    RouteDecisionV1,
    SchedulerKind,
)

SUBMITTED_AT = datetime(2026, 10, 19, 9, 0, 0, tzinfo=timezone.utc)
DEADLINE = SUBMITTED_AT + timedelta(seconds=1_200)
POLICY_VERSION = "route-policy-v1"

JOB_ID = "202610190900000000-aabbccdd"
REGION_ID = "a" * 32
DOC_DIGITAL_ID = "b" * 32
DOC_SCANNED_ID = "c" * 32
DOC_MIXED_ID = "d" * 32
CORRUPT_DOC_ID = "e" * 32
RENDER_ARTIFACT_ID = "f" * 32
OCR_ARTIFACT_ID = "1" * 32
RESULT_ARTIFACT_ID = "2" * 32

TEXT_SHA = "ab" * 32
EMPTY_SHA = "cd" * 32
RENDER_SHA = "ef" * 32
RESULT_SHA = "12" * 32
VALUE_SHA = "34" * 32


def _source(artifact_id: str, sha256: str, size: int) -> ArtifactRef:
    return ArtifactRef(artifact_id=artifact_id, kind="source", sha256=sha256, size_bytes=size)


def engine_policy() -> EnginePolicyV1:
    return EnginePolicyV1(
        allow_fallback=True,
        allow_vlm_challenge=False,
        max_fallback_passes=1,
        page_batch_pages=4,
        deadline_seconds=1_200,
    )


def example_job_request(
    job_id: str = JOB_ID,
    scheduler: SchedulerKind = SchedulerKind.temporal_test,
    allow_vlm_challenge: bool = False,
) -> JobRequestV1:
    return JobRequestV1(
        job_id=job_id,
        scheduler=scheduler,
        tenant_id="tenant-alpha",
        owner_id="owner-principal-7",
        policy_version=POLICY_VERSION,
        engine_policy=EnginePolicyV1(
            allow_fallback=True,
            allow_vlm_challenge=allow_vlm_challenge,
            max_fallback_passes=1,
            page_batch_pages=4,
            deadline_seconds=1_200,
        ),
        documents=[
            DocumentInputV1(
                document_id=DOC_DIGITAL_ID,
                artifact=_source(DOC_DIGITAL_ID, TEXT_SHA, 160_240),
                display_name="RFQ_01_digital.pdf",
            ),
            DocumentInputV1(
                document_id=DOC_SCANNED_ID,
                artifact=_source(DOC_SCANNED_ID, RENDER_SHA, 2_140_000),
                display_name="RFQ_06_scanned.pdf",
            ),
        ],
        submitted_at=SUBMITTED_AT,
        deadline=DEADLINE,
    )


def example_page_manifest(page_number: int = 1, total_pages: int = 3) -> PageManifestV1:
    return PageManifestV1(
        document_id=DOC_SCANNED_ID,
        page_number=page_number,
        total_pages=total_pages,
        classification="scanned",
        source_artifact=_source(DOC_SCANNED_ID, RENDER_SHA, 2_140_000),
        render_artifact=ArtifactRef(artifact_id=RENDER_ARTIFACT_ID, kind="render", sha256=RENDER_SHA, size_bytes=980_000),
        render_transform="dpi=200;max_pixels=6_000_000",
        policy_version=POLICY_VERSION,
    )


def example_unsupported_page_manifest(page_number: int = 2, total_pages: int = 3) -> PageManifestV1:
    return PageManifestV1(
        document_id=CORRUPT_DOC_ID,
        page_number=page_number,
        total_pages=total_pages,
        classification="unsupported",
        source_artifact=_source(CORRUPT_DOC_ID, EMPTY_SHA, 4096),
        error=PageErrorV1(code="decode_failed", message="PDF trailer truncated; page could not be decoded"),
        policy_version=POLICY_VERSION,
    )


def example_route_decision(
    action: str = "legacy_ocr",
    reason_code: str = "no_text_layer",
    attempt: int = 0,
    max_attempts: int = 2,
) -> RouteDecisionV1:
    return RouteDecisionV1(
        job_id=JOB_ID,
        document_id=DOC_SCANNED_ID,
        page_number=1,
        action=action,
        reason_code=reason_code,
        evidence_artifact_ids=[RENDER_ARTIFACT_ID],
        attempt=attempt,
        max_attempts=max_attempts,
        policy_version=POLICY_VERSION,
    )


def example_engine_provenance() -> EngineProvenanceV1:
    return EngineProvenanceV1(
        engine="pp_structure_v3+tesseract",
        engine_version="2.8.1",
        model_version="v3-scanned",
        policy_version=POLICY_VERSION,
    )


def example_completed_page_result(page_number: int = 1, total_pages: int = 3) -> PageResultV1:
    return PageResultV1(
        document_id=DOC_SCANNED_ID,
        page_number=page_number,
        total_pages=total_pages,
        status="completed",
        provenance=example_engine_provenance(),
        confidence=ConfidenceValue(value=0.93),
        text_artifact=ArtifactRef(artifact_id=OCR_ARTIFACT_ID, kind="ocr", sha256=TEXT_SHA, size_bytes=15_360),
        table_artifact=ArtifactRef(artifact_id=RESULT_ARTIFACT_ID, kind="result", sha256=RESULT_SHA, size_bytes=3_840),
        warnings=[],
        field_provenance=[
            FieldProvenanceV1(
                label="rfq_bid_deadline",
                value_sha256=VALUE_SHA,
                row=3,
                column=1,
                evidence="BID DEADLINE: 22/JAN/2027 15:00",
            )
        ],
        checksum=TEXT_SHA,
    )


def example_unresolved_page_result(page_number: int = 1, total_pages: int = 3) -> PageResultV1:
    result = example_completed_page_result(page_number=page_number, total_pages=total_pages)
    return result.model_copy(
        update={"status": "needs_review", "confidence": None, "warnings": ["duplicate label 'deadline' on page; association ambiguous"]}
    )


def example_failed_page_result(page_number: int = 2, total_pages: int = 3) -> PageResultV1:
    result = example_completed_page_result(page_number=page_number, total_pages=total_pages)
    return result.model_copy(update={"status": "failed", "confidence": None, "checksum": EMPTY_SHA})


def example_document_result(order: str = "complete", total_pages: int = 3) -> DocumentResultV1:
    builders = {
        "complete": [example_completed_page_result(n, total_pages) for n in range(1, total_pages + 1)],
        "partial": [example_failed_page_result(n, total_pages) if n == 2 else example_completed_page_result(n, total_pages)
                    for n in range(1, total_pages + 1)],
        "needs_review": [example_unresolved_page_result(n, total_pages) if n == 1 else example_completed_page_result(n, total_pages)
                         for n in range(1, total_pages + 1)],
        "failed": [example_failed_page_result(n, total_pages) for n in range(1, total_pages + 1)],
    }[order]
    return DocumentResultV1(
        job_id=JOB_ID,
        document_id=DOC_SCANNED_ID,
        policy_version=POLICY_VERSION,
        pages=builders,
    )


def example_review_decision(approve: bool = False) -> ReviewDecisionV1:
    if approve:
        return ReviewDecisionV1(
            job_id=JOB_ID,
            result_version=2,
            reviewer_id="reviewer-7",
            decision="approve",
            corrections=[],
            idempotency_key="review-" + "0" * 32,
            submitted_at=SUBMITTED_AT + timedelta(minutes=12),
        )
    return ReviewDecisionV1(
        job_id=JOB_ID,
        result_version=2,
        reviewer_id="reviewer-7",
        decision="correct",
        corrections=[
            CorrectionV1(
                field_ref="b" * 32 + ":1:rfq_bid_deadline",
                replacement="BID DEADLINE: 22/JAN/2027 15:00 IST",
            )
        ],
        idempotency_key="review-" + "1" * 32,
        submitted_at=SUBMITTED_AT + timedelta(minutes=12),
    )
