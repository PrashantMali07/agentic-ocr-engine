"""Typed V1 contracts for the Temporal test lane and LangGraph decisions.

These models are the single vocabulary exchanged between FastAPI admission,
Temporal workflows/activities, the LangGraph decision graphs, and durable
review. Phase 00 freezes them before any scheduling work (Phases 01-03)
depends on their field composition.

Design rules enforced here:

1. Never encode local filesystem paths, server addresses, or document bytes.
   Artifacts are referenced by ID; user-visible names are metadata only.
2. Missing or unknown confidence stays explicit (``None``), never coerced to
   a fabricated value such as ``1.0`` (a real defect recorded in
   SECURITY_REVIEW.md for the interrupt()-based review path).
3. Every decision carries a bounded action and reason code plus the policy
   version that produced it, so replay stays reproducible.
4. Page identity is one-based and every aggregation preserves source order;
   missing pages can never aggregate into a ``completed`` claim.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .ids import (
    DISPLAY_NAME,
    HEX32_ID,
    JOB_ID,
    POLICY_VERSION,
    SHA256,
)

NonEmptyStr = Annotated[
    str,
    BeforeValidator(lambda v: v.strip() if isinstance(v, str) else v),
    Field(min_length=1, max_length=256),
]

# ---------------------------------------------------------------------------
# Job lifecycle — mirrors the legacy JobStatus enum one-to-one. The parity
# test asserts both stay aligned so API polling keeps a single truth.
# ---------------------------------------------------------------------------

class JobStateV1(str, Enum):
    queued = "queued"
    processing = "processing"
    waiting_for_review = "waiting_for_review"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


class SchedulerKind(str, Enum):
    legacy = "legacy"
    temporal_test = "temporal_test"


# ---------------------------------------------------------------------------
# Artifact references — the only document identity that crosses boundaries.
# ---------------------------------------------------------------------------

class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    artifact_id: Annotated[str, Field(pattern=HEX32_ID.pattern)]
    kind: Literal["source", "render", "ocr", "result"]
    sha256: Optional[Annotated[str, Field(pattern=SHA256.pattern)]] = None
    size_bytes: Optional[Annotated[int, Field(ge=0)]] = None


class DocumentInputV1(BaseModel):
    """One stored input document, admitted by the API, referenced by ID."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: Annotated[str, Field(pattern=HEX32_ID.pattern)]
    artifact: ArtifactRef
    display_name: Annotated[str, Field(pattern=DISPLAY_NAME.pattern)]


class PageErrorV1(BaseModel):
    """Explicit error record for an unsupported, corrupt, or truncated page."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Literal[
        "decode_failed",
        "password_protected",
        "environment",
        "render_limit_exceeded",
        "not_supported",
        "unknown",
    ]
    message: Annotated[str, Field(min_length=1, max_length=512)]


# ---------------------------------------------------------------------------
# JobRequestV1 — server-generated identity + bounded engine policy.
# ---------------------------------------------------------------------------

class EnginePolicyV1(BaseModel):
    """Bounded extraction policy fixed at admission time."""

    model_config = ConfigDict(extra="forbid")

    allow_fallback: bool = True
    allow_vlm_challenge: bool = False
    max_fallback_passes: Annotated[int, Field(ge=0, le=2)] = 1
    page_batch_pages: Annotated[int, Field(ge=1, le=16)] = 4
    deadline_seconds: Annotated[int, Field(ge=60, le=3_600)] = 1_200

    @model_validator(mode="after")
    def bounded_passes(self) -> "EnginePolicyV1":
        if self.max_fallback_passes == 0 and self.allow_fallback:
            raise ValueError("allow_fallback requires max_fallback_passes >= 1")
        return self


class JobRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: Annotated[str, Field(pattern=JOB_ID.pattern)]
    scheduler: SchedulerKind = SchedulerKind.temporal_test
    tenant_id: NonEmptyStr
    owner_id: NonEmptyStr
    policy_version: Annotated[str, Field(pattern=POLICY_VERSION.pattern)]
    engine_policy: EnginePolicyV1
    documents: Annotated[List[DocumentInputV1], Field(min_length=1, max_length=8)]
    submitted_at: datetime
    deadline: datetime

    @field_validator("documents")
    @classmethod
    def unique_documents(cls, value: List[DocumentInputV1]) -> List[DocumentInputV1]:
        ids = [doc.document_id for doc in value]
        if len(set(ids)) != len(ids):
            raise ValueError("Each document must have a unique document_id")
        return value

    @model_validator(mode="after")
    def deadline_after_submission(self) -> "JobRequestV1":
        if self.deadline <= self.submitted_at:
            raise ValueError("deadline must be later than submitted_at")
        return self


# ---------------------------------------------------------------------------
# PageManifestV1 — one-based page identity emitted by preflight/planning.
# ---------------------------------------------------------------------------

class PageManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: Annotated[str, Field(pattern=HEX32_ID.pattern)]
    page_number: Annotated[int, Field(ge=1, le=10_000)]
    total_pages: Annotated[int, Field(ge=1, le=10_000)]
    classification: Literal["digital", "scanned", "mixed", "image", "unsupported"]
    source_artifact: ArtifactRef
    render_artifact: Optional[ArtifactRef] = None
    render_transform: Optional[Annotated[str, Field(max_length=256)]] = None
    error: Optional[PageErrorV1] = None
    policy_version: Annotated[str, Field(pattern=POLICY_VERSION.pattern)]

    @model_validator(mode="after")
    def bounded_page(self) -> "PageManifestV1":
        if self.page_number > self.total_pages:
            raise ValueError("page_number must not exceed total_pages")
        if self.error is not None and self.classification == "digital":
            raise ValueError("A page with an error record cannot be classified digital")
        if self.error is None and self.classification == "unsupported":
            raise ValueError("An unsupported page requires an explicit error record")
        return self


# ---------------------------------------------------------------------------
# RouteDecisionV1 — bounded LangGraph route/audit output.
# ---------------------------------------------------------------------------

RouteActionV1 = Literal["native", "legacy_ocr", "fallback", "vlm_challenge", "review", "fail"]

REASON_CODES_V1 = frozenset({
    "policy_default",
    "digital_text_layer",
    "no_text_layer",
    "low_ocr_confidence",
    "confidence_unknown",
    "missing_critical_field",
    "ambiguous_table_association",
    "page_decode_failed",
    "output_limit_exceeded",
    "challenge_agrees",
    "challenge_disagrees",
    "exhausted_passes",
    "unresolved_page",
    "unknown",
})


class RouteDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: Annotated[str, Field(pattern=JOB_ID.pattern)]
    document_id: Annotated[str, Field(pattern=HEX32_ID.pattern)]
    page_number: Annotated[int, Field(ge=1, le=10_000)]
    action: RouteActionV1
    reason_code: str
    evidence_artifact_ids: Annotated[List[str], Field(default_factory=list, max_length=16)]
    attempt: Annotated[int, Field(ge=0, le=10)]
    max_attempts: Annotated[int, Field(ge=1, le=10)]
    policy_version: Annotated[str, Field(pattern=POLICY_VERSION.pattern)]
    notes: Optional[Annotated[str, Field(max_length=512)]] = None

    @field_validator("reason_code")
    @classmethod
    def bounded_reason(cls, value: str) -> str:
        if value not in REASON_CODES_V1:
            raise ValueError(
                f"Unregistered reason_code {value!r}; use REASON_CODES_V1 so decisions stay replayable"
            )
        return value

    @field_validator("evidence_artifact_ids")
    @classmethod
    def bounded_evidence(cls, value: List[str]) -> List[str]:
        for artifact_id in value:
            if not HEX32_ID.fullmatch(artifact_id):
                raise ValueError(f"Evidence reference {artifact_id!r} must be a hex32 artifact ID")
        return value

    @model_validator(mode="after")
    def attempt_within_bounds(self) -> "RouteDecisionV1":
        if self.attempt >= self.max_attempts:
            raise ValueError("attempt must be < max_attempts to keep the decision loop finite")
        return self


# ---------------------------------------------------------------------------
# PageResultV1 — one terminal outcome per page, with provenance.
# ---------------------------------------------------------------------------

PageStatusV1 = Literal["completed", "partial", "needs_review", "failed", "unsupported"]

class ConfidenceValue(BaseModel):
    """Measured OCR/model confidence. Absence of this record is NOT a pass:
    the result is incomplete until review resolves the unknown."""

    model_config = ConfigDict(extra="forbid")

    value: Annotated[float, Field(ge=0.0, le=1.0)]


class FieldProvenanceV1(BaseModel):
    """Where a scored value came from: label, cell coordinates, evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: Annotated[str, Field(min_length=1, max_length=128)]
    value_sha256: Annotated[str, Field(pattern=SHA256.pattern)]
    row: Optional[Annotated[int, Field(ge=0)]] = None
    column: Optional[Annotated[int, Field(ge=0)]] = None
    evidence: Optional[Annotated[str, Field(max_length=256)]] = None


class EngineProvenanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    engine: Annotated[str, Field(min_length=1, max_length=64)]
    engine_version: Optional[Annotated[str, Field(max_length=64)]] = None
    model_version: Optional[Annotated[str, Field(max_length=64)]] = None
    policy_version: Annotated[str, Field(pattern=POLICY_VERSION.pattern)]


class PageResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: Annotated[str, Field(pattern=HEX32_ID.pattern)]
    page_number: Annotated[int, Field(ge=1, le=10_000)]
    total_pages: Annotated[int, Field(ge=1, le=10_000)]
    status: PageStatusV1
    provenance: EngineProvenanceV1
    confidence: Optional[ConfidenceValue] = None  # None == explicitly unknown; never a pass.
    text_artifact: Optional[ArtifactRef] = None
    table_artifact: Optional[ArtifactRef] = None
    warnings: Annotated[List[Annotated[str, Field(max_length=256)]], Field(default_factory=list, max_length=20)]
    field_provenance: Annotated[List[FieldProvenanceV1], Field(default_factory=list, max_length=64)]
    checksum: Annotated[str, Field(pattern=SHA256.pattern)]

    @model_validator(mode="after")
    def failed_pages_reveal_nothing(self) -> "PageResultV1":
        if self.status in {"failed", "unsupported"} and self.confidence is not None:
            raise ValueError("A failed/unsupported page must not claim measured confidence")
        return self


class DocumentResultV1(BaseModel):
    """Ordered aggregation of one document's page results.

    Missing pages prevent a completed claim: contiguity is validated here so
    no later stage can silently drop a page and present partial output as
    success."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: Annotated[str, Field(pattern=JOB_ID.pattern)]
    document_id: Annotated[str, Field(pattern=HEX32_ID.pattern)]
    policy_version: Annotated[str, Field(pattern=POLICY_VERSION.pattern)]
    pages: Annotated[List[PageResultV1], Field(min_length=1, max_length=10_000)]

    @model_validator(mode="after")
    def contiguous_order(self) -> "DocumentResultV1":
        expected = 1
        declared_total = self.pages[0].total_pages
        for page in self.pages:
            if page.page_number != expected:
                raise ValueError(
                    f"pages must be contiguous one-based source order; expected page {expected}, got {page.page_number}"
                )
            if page.document_id != self.document_id:
                raise ValueError("pages must all reference the same document_id")
            if page.total_pages != declared_total:
                raise ValueError("pages must agree on the document's total_pages")
            expected += 1
        if len(self.pages) != declared_total:
            raise ValueError("every declared page needs an explicit result")
        return self

    @property
    def outcome(self) -> Literal["completed", "partial", "needs_review", "failed"]:
        if all(p.status == "completed" for p in self.pages):
            return "completed"
        if any(p.status in {"failed", "unsupported"} for p in self.pages):
            return "failed"
        if any(p.status == "needs_review" for p in self.pages):
            return "needs_review"
        return "partial"


# ---------------------------------------------------------------------------
# ReviewDecisionV1 — durable, authorized, idempotent review input.
# ---------------------------------------------------------------------------

class SpanV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=1)]


class CorrectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field_ref: Optional[Annotated[str, Field(max_length=256)]] = None
    span: Optional[SpanV1] = None
    expected: Optional[Annotated[str, Field(min_length=1, max_length=10_000)]] = None
    replacement: Annotated[str, Field(max_length=10_000)]

    @model_validator(mode="after")
    def span_matches_expected(self) -> "CorrectionV1":
        if (self.span is None) != (self.expected is None):
            raise ValueError("A span correction requires both span and expected text")
        if self.span is not None:
            if self.span.end <= self.span.start:
                raise ValueError("Span must satisfy end > start")
            expected_text = self.expected or ""
            if self.span.end - self.span.start != len(expected_text):
                raise ValueError("Span length must equal expected text length")
        return self


class ReviewDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: Annotated[str, Field(pattern=JOB_ID.pattern)]
    result_version: Annotated[int, Field(ge=0)]
    reviewer_id: NonEmptyStr
    decision: Literal["approve", "correct"]
    corrections: Annotated[List[CorrectionV1], Field(default_factory=list, max_length=50)]
    idempotency_key: Annotated[str, Field(min_length=16, max_length=128)]
    submitted_at: datetime
    notes: Optional[Annotated[str, Field(max_length=512)]] = None

    @model_validator(mode="after")
    def has_content(self) -> "ReviewDecisionV1":
        if self.decision == "correct" and not self.corrections:
            raise ValueError("decision 'correct' requires at least one correction")
        if self.decision == "approve" and self.corrections:
            raise ValueError("decision 'approve' must not carry corrections")
        return self
