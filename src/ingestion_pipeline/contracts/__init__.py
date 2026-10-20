"""V1 ingestion contracts (Phase 00 frozen).

Import path: ``ingestion_pipeline.contracts``.

Owned state transitions (single-owner rule):

- FastAPI validates and records admission (builds ``JobRequestV1``).
- Temporal drives execution and durable review waits (consumes
  ``JobRequestV1``, emits ``PageManifestV1``/``PageResultV1``, waits for
  ``ReviewDecisionV1`` signals).
- Activities write idempotent page artifacts/results.
- LangGraph returns ``RouteDecisionV1``/audit decisions; it never schedules
  GPU work or owns a lifecycle.
- API review endpoints authorize the reviewer, record the event, then signal
  Temporal.

Nothing in this package imports application internals, so contracts cross
process boundaries without loading GPU/OCR dependencies.
"""

from .bounds import (
    DEFAULT_METADATA_BUDGET_BYTES,
    MetadataBudgetError,
    assert_metadata_budget,
)
from .examples import (
    example_completed_page_result,
    example_document_result,
    example_engine_provenance,
    example_failed_page_result,
    example_job_request,
    example_page_manifest,
    example_review_decision,
    example_route_decision,
    example_unresolved_page_result,
    example_unsupported_page_manifest,
)
from .ids import (
    BOUNDED_ID,
    DISPLAY_NAME,
    HEX32_ID,
    JOB_ID,
    POLICY_VERSION,
    SHA256,
    is_display_name,
    is_hex32_id,
    is_job_id,
    is_policy_version,
    is_sha256,
)
from .models import (
    REASON_CODES_V1,
    ArtifactRef,
    ConfidenceValue,
    CorrectionV1,
    DocumentInputV1,
    DocumentResultV1,
    EnginePolicyV1,
    EngineProvenanceV1,
    FieldProvenanceV1,
    JobRequestV1,
    JobStateV1,
    PageErrorV1,
    PageManifestV1,
    PageResultV1,
    ReviewDecisionV1,
    RouteDecisionV1,
    SchedulerKind,
    SpanV1,
)

__all__ = [
    "DEFAULT_METADATA_BUDGET_BYTES",
    "MetadataBudgetError",
    "assert_metadata_budget",
    "BOUNDED_ID",
    "DISPLAY_NAME",
    "HEX32_ID",
    "JOB_ID",
    "POLICY_VERSION",
    "SHA256",
    "is_display_name",
    "is_hex32_id",
    "is_job_id",
    "is_policy_version",
    "is_sha256",
    "ArtifactRef",
    "ConfidenceValue",
    "CorrectionV1",
    "DocumentInputV1",
    "DocumentResultV1",
    "EnginePolicyV1",
    "EngineProvenanceV1",
    "FieldProvenanceV1",
    "JobRequestV1",
    "JobStateV1",
    "PageErrorV1",
    "PageManifestV1",
    "PageResultV1",
    "REASON_CODES_V1",
    "ReviewDecisionV1",
    "RouteDecisionV1",
    "SchedulerKind",
    "SpanV1",
    "example_completed_page_result",
    "example_document_result",
    "example_engine_provenance",
    "example_failed_page_result",
    "example_job_request",
    "example_page_manifest",
    "example_review_decision",
    "example_route_decision",
    "example_unresolved_page_result",
    "example_unsupported_page_manifest",
]
