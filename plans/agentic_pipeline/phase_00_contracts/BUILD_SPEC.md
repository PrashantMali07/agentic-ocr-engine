# Phase 00 build spec — contracts and baseline

Estimate: **2–3 engineer-days**. Depends on the existing supported legacy API and fixtures. Produces the contracts consumed by every later phase.

## Interfaces to define

- `JobRequestV1`: server-generated `job_id`, authenticated `tenant_id`/`owner_id`, source artifact IDs and hashes, bounded engine policy, submission timestamp, deadline, and policy version. The request may carry user-visible filenames as metadata; they are never file paths or workflow IDs.
- `PageManifestV1`: document ID, one-based page number, total pages, format/digital-scan classification, source hash, optional render transform, and artifact reference. An unsupported/corrupt page has an explicit error record.
- `RouteDecisionV1`: action (`native`, `legacy_ocr`, `fallback`, `vlm_challenge`, `review`, `fail`), reason code, evidence references, attempt count, and policy version. The graph cannot return arbitrary activity names or unbounded recursion.
- `PageResultV1`: document/page identity, engine/model versions, result artifact IDs, status, warnings, confidence with an `unknown` state, table/field provenance, and checksum. Aggregation preserves page order and reports missing pages.
- `ReviewDecisionV1`: target job/result version, reviewer identity from verified auth, correction payload, and idempotency key; repeated/stale review cannot overwrite a newer result.

Use typed models in a new `src/ingestion_pipeline/contracts/` package or the existing `schema/` package after checking import boundaries. Define JSON compatibility and migration rules before wiring workers. Temporal payloads contain contract metadata and artifact IDs, not page bytes, OCR transcripts, or raw VLM responses.

## Baseline inventory

Record the current `/extract/v1` statuses and result shape from `api.py`, `schema/responses.py`, `job_store.py`, and `services/job_runner.py`. Freeze a fixture manifest with source hashes and format/page counts for digital, scanned, mixed multipage, image, malformed, and oversized documents. The five RFQ scans in `test_asset` are the initial scanned subset, not a representative production corpus. Record observed legacy output as comparison evidence without treating it as truth.

## Acceptance

Contract examples serialize/deserialize across process boundaries. The status mapping covers `queued`, `processing`, `waiting_for_review`, `completed`, `partial`, `failed`, `cancelled`, `timed_out`, and expiry. A validation test rejects missing tenant/artifact IDs, invalid page indices, unbounded actions, stale review versions, and payloads over the chosen metadata limit. The fixture manifest is hash-bound and private document text is not committed.
