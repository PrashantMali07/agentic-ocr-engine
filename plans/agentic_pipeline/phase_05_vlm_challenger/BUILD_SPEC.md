# Phase 05 build spec — runtime VLM challenger in test lane

Estimate: **4–7 engineer-days** after Phase 03; quality comparison also depends on Phase 04 reviewed labels. The test lane includes this branch, but the supported API keeps it disabled.

## Adapter and policy

- Define a local `VlmCandidateV1` adapter accepting a tenant-scoped page-image artifact ID, page number/hash, model/prompt version, and deadline. Return bounded structured text, table/field candidates, uncertainty flags, evidence coordinates or excerpts where available, and a result artifact ID. Reject free-form tool requests and arbitrary URLs/paths.
- Use a separate queue/capacity budget from OCR, with hard timeouts, output byte limits, deterministic settings where supported, and cleanup. If OCR and VLM share one GPU, schedule them to avoid simultaneous model residency/OOM; measure the tradeoff. The model/weights must be pinned and locally available for the test run.
- LangGraph may request `vlm_challenge` only for a finite set of reasons: low OCR quality, missing critical field, ambiguous table association, or policy-selected sampling. Temporal dispatches the VLM activity and returns the candidate reference to graph reconciliation.
- Reconciliation compares candidates and image-grounded evidence. Agreement is not automatically truth; disagreement or unsupported field association leads to review. Store original OCR and VLM candidates separately with provenance. Never overwrite a monetary amount or identifier without an authorized review decision.
- Failure or timeout of VLM yields an explicit warning/review path; it never converts a failed OCR result to complete success. Tenant/artifact access and raw-response logging follow the same security limits as OCR.

Likely new modules: `src/ingestion_pipeline/vlm/adapter.py`, `schema.py`, `policy.py`, plus Temporal VLM activity/worker and graph reconciliation changes. Keep model-specific code behind the adapter for later replacement. No hosted VLM call is included without data-owner approval.

## Acceptance

Test with a stub runner first, then a real approved local model. Verify schema rejection, timeout, queue saturation, isolation, retry idempotency, model provenance, and no document text in routine logs/history. On the frozen Phase 04 holdout, compare OCR-only and OCR+VLM per-page text, field association, false-review rate, p95 latency, peak VRAM, and failure rate. Record cases where VLM helps and harms. Promotion requires owner-approved thresholds and resource gates; the experiment may validly conclude “leave disabled.”
