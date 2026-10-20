# V1 contracts — compatibility map and open decisions

Status: **V1 frozen** (Phase 00). These models are the required vocabulary
for Phases 01–05. Changes after this freeze follow the migration rules at
the end of this document.

## State-transition ownership (single-owner rule)

| Transition | Owner | Contract | Notes |
| --- | --- | --- | --- |
| Admission, auth, upload validation, queue reservation | FastAPI `/extract/v1` + `JobStore.admit` | `JobRequestV1` built server-side after upload | Caller cannot choose scheduler kind beyond lane selection in Phase 02. |
| Job claim and retry counting | Legacy: `JobStore.claim_next` (lease). Temporal test lane: workflow start from admission outbox (Phase 02) | `JobRequestV1` | The legacy claim query must exclude temporal-lane jobs once the scheduler column exists. |
| Extraction execution | Activities (Phase 01 shared service) | consume `PageManifestV1`, emit `PageResultV1` | Idempotent writes keyed by `(job, page, stage, policy_version)`. |
| Route/audit decisions | LangGraph (Phase 03) | emit `RouteDecisionV1` only | Bounded actions and reason codes; no scheduling, no lifecycle. |
| Durable review wait | Temporal (Phase 02) | wait on `ReviewDecisionV1` signal | Graph `interrupt()` is not a production review lifecycle. |
| Review authorization and event record | FastAPI review endpoints | build `ReviewDecisionV1` from authenticated `Principal` | Existing `review_events` table already stores `(job_id, version)` events with before/after results. |
| Status polling | FastAPI + job store | legacy `JobStatusResponse` | Contract `JobStateV1` mirrors legacy `JobStatus` one-to-one (parity-tested). |

## Compatibility with the existing surfaces (what must not break)

- **Statuses.** `JobStateV1` carries exactly the eight legacy values
  (`queued`, `processing`, `waiting_for_review`, `completed`, `partial`,
  `failed`, `cancelled`, `timed_out`). Expiry is applied on read by the
  existing TTL path (`JOB_TTL` 24 h, `REVIEW_TTL` 7 d) rather than by adding
  a ninth state.
- **Review API shape.** `schema/requests.ReviewCorrection` (span-based) maps
  to `CorrectionV1(span, expected, replacement)`; `ReviewRequest.result_version`
  maps to `ReviewDecisionV1.result_version`, which continues to feed the
  `review_version` optimistic-concurrency check in `JobStore.submit_review`.
- **Filenames.** Existing clients key results by original filename; the
  contract records that as `DocumentInputV1.display_name` (metadata only,
  path-separator regex rejects `/\<>`). Server/job identity uses the
  server-generated `job_id` regex, matching current timestamp-hex job IDs.
- **Manifest shape.** `PageManifestV1.document_id` continues the existing
  hex32 document-ID/storage layout used by `services/job_runner.py`.
- **Result shape.** Page-level results live under artifact IDs;
  `DocumentContent` in `schema/responses.py` remains the API-visible shape
  until a versioned migration (Phase 06) is approved.
- **Admission limits.** `EnginePolicyV1` bounds (1–8 documents, batch 1–16
  pages, ≤2 fallback passes, deadline 60–3600 s) are compatible with the
  existing single-host caps (`MAX_PENDING_JOBS` 8, ≤8 attachments, worker
  lease + `MAX_ATTEMPTS` 2).
- **Heavy-import isolation.** `contracts/` imports only pydantic/stdlib, so
  Temporal activities and the LangGraph graphs can consume it without
  loading the Paddle stack (existing `schema.responses` imports
  `extraction.manager`, which pulls OCR engines — contracts deliberately do
  not import from `schema/`).

## Known gaps intentionally left to later phases

- `JobRequestV1` includes `scheduler` (legacy/temporal_test) so Phase 02 can
  add its SQLite column and claim-query filter without a second freeze.
- Corpus above is classification *evidence*, not accuracy ground truth: see
  `phase00_evidence/fixture_manifest.json` (private, hash-bound).

## Migration rules after the V1 freeze

1. Additive fields only within V1; every change must carry a documented
   reason and updated round-trip tests.
2. Any break (field rename,removal, semantic change) bumps `V1` to `V2`
   files alongside a compatibility shim; never mutate V1 in place.
3. `REASON_CODES_V1` is the bounded vocabulary for decision traces; new
   codes require a paired policy version bump (`route-policy-v1` → `v2`).

## Open decisions (tracked, not blockers for Phase 01)

| Decision | Owner | Needed by |
| --- | --- | --- |
| Frozen holdout split (hash-listed) for accuracy scoring | engineering with business owner | Phase 04 scoring |
| Local VLM model + runner contract (pinned weights/prompt) | engineering | Phase 04/05 |
| GPU budget & contention policy with OCR worker | operations/engineering | Phase 06 soak |
| Distributed topology (shared storage/transactional job store) | engineering | before any multi-host rollout |
| Accuracy thresholds (CER/WER/association/review rate) | business owner | before Phase 06 scoring |
