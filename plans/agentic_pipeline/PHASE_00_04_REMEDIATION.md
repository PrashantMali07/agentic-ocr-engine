# Phase 00–04 review remediation

Status: **plan**, 20 October 2026. This resolves findings from the Phase 00–04 review before claiming those phases complete. It changes neither production traffic nor the user's instruction to avoid image builds and deployment. Baseline at review: 140 repository tests passed, two live Temporal tests skipped; passing tests do not cover the defects below.

Implementation update, 21 October 2026: The owner asked to defer change set 1's VLM cloud boundary. Code for page accounting, atomic API/Temporal admission, the reconciliation loop, cancellation, versioned review replay, and the bounded stateless LangGraph route/audit path is now in place. The new embedded Temporal integration test covers API admission, a stopped and restarted worker, provisional result delivery, review signal replay, and API polling. **The full suite reports 159 passed and two explicitly opted-out external live tests.** The older external live Temporal/GPU suite stalled when it auto-ran, so it now requires `RUN_LIVE_TEMPORAL_TESTS=true`; it remains unverified on the intended host. No image build or deployment was run. Phase 04 accuracy acceptance still requires independent image review and owner-approved thresholds. These are separate from code completion.

## Completion target

An authenticated FastAPI test submission creates a Temporal-only job atomically; the legacy worker cannot claim it. A real Temporal worker completes every planned page through the shared extractor, LangGraph makes bounded route/audit decisions, workflow state reconciles to API polling, authorized review resumes once, and incomplete/failed pages cannot become completed. VLM label preparation records its configured provider and model and produces reviewer-verified references before accuracy acceptance.

## Change set 1 — Verify VLM provenance and bound runner output (1–2 engineer-days)

**Problem:** `vlm/config.py` defaults Ollama to `gemma4:31b-cloud`. `phase04_review/oct20_run/vlm_drafts/drafts.json` records that cloud model ID; a local Ollama endpoint can proxy a `-cloud` model to hosted inference. `prepare_vlm_review.py` also captures runner stdout/stderr without a streaming byte cap. The owner has accepted cloud inference for synthetic documents and will select a local model before testing real documents; no cloud-permission flag is required.

**Code changes:** Bind the recorded model ID/provenance to the runner's actual configuration and weight/version where available; do not accept a free-form `--model-id` as proof of the model used. Stream or otherwise bound both runner stdout and stderr while the child runs, kill it on output/deadline excess, and keep drafts private.

**Existing evidence:** verify the actual provider/model configuration used for the October 20 draft run without exposing secrets. Record whether it was hosted. A completed cloud call cannot be undone by changing code.

**Proof:** tests for model/provenance mismatch, oversized output, timeout, and no document bytes in routine logs. No new external VLM call is required for these tests.

## Change set 2 — Make page/result contracts truthful (1 engineer-day)

**Problem:** `contracts/models.py::DocumentResultV1` accepts a one-page list whose page says `total_pages=3` and reports `completed`.

**Code changes:** require all page records to agree on `total_pages`; require unique, contiguous one-based pages and `len(pages) == total_pages` before a completed document can exist. For an interrupted run, either synthesize explicit failed/missing page results before aggregation or reject the incomplete document and persist a failure. Keep `partial`, `needs_review`, and `failed` distinct in the API/job store; do not map an empty page to completed.

**Proof:** regression tests for missing final page, middle-page gap, duplicate page, inconsistent totals, mixed complete/failed pages, and an all-complete document. The previously reproduced `1 of 3 -> completed` case must fail validation or report incomplete.

## Change set 3 — Finish Temporal admission, result delivery, and review (4–6 engineer-days)

**Admission:** replace separate `admit` then `stage_lane_submission` transactions with one `admit_temporal_test` transaction that reserves capacity and inserts the job with `scheduler_kind='temporal_test'` plus its outbox request. Keep existing rows and ordinary `/extract/v1` requests on `legacy`. Expose the lane only behind an explicit test feature flag and appropriate authenticated role; callers cannot choose server, namespace, file paths, or workflow ID. Start/reconcile the outbox in a dedicated supervised loop, not only from an ad hoc helper. The legacy claim query must exclude lane jobs from their first committed state.

**Results:** make the workflow query or a dedicated persisted summary return `job_id`, page outcomes, and checksummed artifact references required by `bridge.py`; keep it bounded and text-free. The reconciler must actually run, read artifacts by tenant/job-scoped ID, and deliver provisional review and terminal results idempotently. Record delivery state/version so polling does not repeatedly rebuild settled results. Map `partial`, `failed`, `cancelled`, and `timed_out` without collapsing them to `completed`. Propagate API cancellation to Temporal and keep already-running activities from committing a late success.

**Review:** persist a versioned review event first, then signal Temporal with only an event ID/version. Reconcile unsent/unacknowledged events after crashes. Validate signal version/idempotency before resuming. A signal never changes a job with failed pages to completed; final status is recomputed from all page outcomes and applied corrections. The API's corrected result remains authoritative and cannot be overwritten by late workflow delivery.

**Proof:** a real API + Temporal test covers crash between admission/start, duplicate start, legacy-worker race, status-query-to-bridge delivery, API polling, cancellation, worker restart, late activity result, review signal loss/retry, stale review, review of one of several documents, and failed-page-plus-review status. At least one test must call `sync_one_workflow` with the real query shape rather than a hand-built final payload.

## Change set 4 — Implement the LangGraph decision component (4–6 engineer-days)

**Problem:** the current Temporal workflow dispatches activities directly; the existing graph still holds `numpy` images and raw OCR results in state, uses `MemorySaver`, and owns a separate interrupt-based review path.

**Code changes:** implement small typed route/audit graph invocations over `PageManifestV1`, artifact IDs, bounded quality metrics, and `RouteDecisionV1`. Keep OCR, rendering, Tesseract, and VLM work in Temporal activities through the shared service. Temporal validates each graph action and dispatches it; the graph cannot start jobs, choose arbitrary tools, or wait for review. Enforce an attempt limit (initial extraction plus bounded fallback/challenge) and fail closed on unknown confidence, audit exceptions, missing artifacts, or inconsistent page identity. Restrict the old image-filled graph to development compatibility or migrate its callers; do not use its `MemorySaver` in the Temporal lane.

**Proof:** branch tests for digital, clean scan, low confidence, fallback, ambiguous table/field association, audit failure, missing page, and human review. Integration tests assert the Temporal workflow actually invokes graph decisions, action traces contain reason codes but no document text, and repeated actions stop at the configured limit.

## Change set 5 — Close Phase 04 labels and live evidence (2–4 engineer-days plus reviewers)

**Code/test work:** add adversarial tests for `prepare_vlm_review.py` manifest tampering, page swap, malformed/oversized runner output, timeout, duplicate fields, and incomplete review. Keep VLM draft, reviewed reference, and OCR outputs separate. Add a meaningful holdout with varied formats/quality if broad accuracy claims are intended; the five RFQ scans only validate the initial method.

**Human gates:** the October 20 review template currently has no reviewer identity/date and zero image-verified pages or fields. An independent reviewer must verify every full transcript and label/row/column association from the source images, then finalize a hash-bound reference. The business owner must set thresholds before pass/fail scoring. These are not engineering tasks to mark complete on the owner's behalf.

**Live proof:** run the two currently skipped Temporal tests against a local test server and add a real API-to-worker restart/cancel/review scenario. Do not run container builds or deployment. Report the live server version, test command, pass/fail evidence, and any GPU model substitution used in the test.

## Order, estimate, and gates

| Order | Change set | Reason it comes here | Engineer estimate |
| --- | --- | --- | ---: |
| 1 | VLM data boundary | Prevent another unintended hosted document call. | 1–2 days |
| 2 | Page/result contract | Later orchestration depends on truthful status. | 1 day |
| 3 | Temporal admission/delivery/review | Establish one durable end-to-end lifecycle. | 4–6 days |
| 4 | LangGraph decisions | Integrate a bounded router with that lifecycle. | 4–6 days |
| 5 | Phase 04 tooling/live validation | Prove behavior and prepare independent acceptance. | 2–4 days |

**Total code/test remediation: approximately 12–19 engineer-days (about 2½–4 working weeks for one engineer).** Independent reviewer and business-owner time is additional and may be the calendar critical path. Phase 05 runtime VLM comparison and Phase 06 release validation resume only after these foundations pass. The current supported legacy route remains unchanged until a later release decision.

The Phase 00–04 closure report must say separately: code fixed and tested; live Temporal durability verified; independent accuracy reference signed; thresholds approved; and any hosted-model data handling resolved. Do not combine them into one “done” label.
