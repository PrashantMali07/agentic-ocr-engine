# Legacy OCR, LangGraph, Temporal, and VLM implementation plan

Status: **proposed implementation plan**, 19 October 2026. No build, deployment, or production route switch is authorized by this document. The current supported path remains the authenticated FastAPI `/extract/v1` legacy engine and its single-host SQLite worker.

The complete phase-by-phase build specifications, execution plans, and design rationales are indexed in [`plans/agentic_pipeline/README.md`](plans/agentic_pipeline/README.md).

## Goal and role of each component

| Component | Responsibility in the target design | Boundary |
| --- | --- | --- |
| Legacy PP-StructureV3 + Tesseract pipeline | Baseline extraction for digital and scanned documents, tables, reading order, and fallback reconciliation | Reuse the same bounded parser/page services in both current and experimental paths; preserve provenance and original values. |
| Temporal | Durable job/page orchestration: queueing, bounded concurrency, retries, timeouts, cancellation, progress, and waiting for review | Coordinates work by artifact reference; it does not parse a large file or keep page images and full OCR output in workflow history. |
| LangGraph | Bounded decision and verification graph: select extraction action from preflight signals, inspect OCR quality, request fallback/VLM/review, and explain the choice | Runs as a CPU activity/subroutine, returns a small typed decision. It does not independently schedule OCR jobs, own production retries, or pause for human review. |
| VLM, offline | Draft transcripts and field/table labels for a human-verified accuracy corpus | Never becomes ground truth without image-based independent review. See `VLM_TEST_SUITE.MD`. |
| VLM, runtime test experiment | Produce a second candidate for selected low-quality pages and a disagreement report | Included in the isolated test lane and disabled in the supported API. It can be promoted only after accuracy, latency, privacy, and GPU-capacity gates. It cannot silently replace a business-critical value. |

The target path is:

```text
authenticated FastAPI request
  -> admission + bounded upload + job/artifact records
  -> one Temporal workflow per admitted job
  -> preflight and page manifest (CPU)
  -> for each bounded page batch:
       LangGraph route decision (CPU)
       -> native/digital extraction (CPU) OR legacy OCR (GPU + CPU fallback)
       -> VLM candidate in the test lane when the route policy requests it (separate bounded queue)
       -> LangGraph quality/association audit (CPU)
  -> aggregate ordered pages + explicit partial failures
  -> authorized human review if needed (Temporal waits)
  -> versioned final result in job store; API polling returns it
```

Temporal limits simultaneous jobs and page tasks, but large-file safety comes from upload/preflight limits, storage quotas, bounded page batches, worker process limits, and cleanup. Use one scheduling authority per job: a Temporal job is never claimed by the current SQLite worker. For a single-host test lane, shared local storage and SQLite are sufficient; multiple hosts need shared artifact storage and a transactional shared job store before rollout.

## Existing baseline and gaps

- `/extract/v1` accepts only `legacy`; the LangGraph route is a separate gated synchronous route. Keep the supported route as the comparator until the new path passes its gates.
- `temporal/client.py` currently passes two positional workflow arguments through `start_workflow`; repair its SDK call and replace caller-controlled path/server/namespace with configured values and a server-generated job identity.
- The current Temporal workflow and activities are not reconciled with API job status/results/review. They also pass large OCR data through workflow activity payloads and process only page 0 of a scanned PDF.
- `graph/workflow.py` uses `MemorySaver`; `graph/state.py` carries image arrays and raw OCR intermediates. The current graph is deterministic routing, not a VLM agent. Its internal review interrupt is not a durable production review lifecycle.
- Phase 4 accuracy sign-off remains a separate human/business-owner gate. A runtime VLM cannot substitute for that independent acceptance process.

## Design contracts to fix before implementation

1. Define a versioned `JobRequest` (job ID, tenant/owner, artifact IDs, source hashes, engine policy, deadlines) and a `PageManifest` (stable one-based page IDs, format, render metadata). Do not place local absolute paths or document text in workflow IDs or routine logs.
2. Define typed `RouteDecision` (`native`, `legacy_ocr`, `fallback`, `vlm_challenge`, `review`, `fail`) with reason codes, evidence references, maximum passes, and a policy version. Make uncertain or missing confidence explicit.
3. Define a `PageResult` with source/page IDs, engine/model versions, text/table artifact references, extraction confidence, warnings, provenance, and terminal/partial status. The final result references page results in source order.
4. Define the only owner of each state transition. FastAPI validates and records admission; Temporal drives execution and review waits; activities atomically write idempotent artifacts/results; API review endpoints authorize and record corrections before signaling Temporal.
5. Decide the first topology: single-host local test lane, one GPU OCR worker process, and separately bounded VLM capacity for the runtime experiment. PostgreSQL/object storage is a later distributed-topology requirement, not a prerequisite for an isolated local prototype.

## Work packages, dependencies, and exit checks

These estimates are **hands-on engineering days for one engineer** who already knows this repository, with a working local GPU and Temporal test environment. They include code, focused tests, and documentation, but not waiting for human labels, model approval, image builds, deployment, or production operator configuration. Ranges reflect parser edge cases and integration failures, not a promise of a release date.

| Package | Estimate | Implementation | Exit check |
| --- | ---: | --- | --- |
| A. Contract and fixture baseline | 2–3 days | Inventory current API/result/review schemas; freeze representative digital, scanned, mixed, multipage, and corrupt fixtures; write typed job/page/decision contracts and version rules. | Contract examples cover complete, partial, review, failure, retry, and cancellation. |
| B. Shared bounded extraction service | 5–7 days | Extract the current legacy parser into a common format/page service; full scanned/mixed PDF page iteration; page provenance; bounded rendering and result writes; reuse it from the current worker and experimental activities. | The same fixture produces equivalent page counts, text/table output, and errors through both callers; no first-page-only behavior. |
| C. Temporal test lane | 6–9 days | Repair client submission; configured namespace/queues; server-generated idempotent workflow ID; admission-to-start reconciliation; artifact references; CPU/GPU worker limits; retries/timeouts/cancellation; job-store progress/result bridge and restart recovery. | Real Temporal integration test proves submit, restart, retry, cancellation, duplicate submission, and no stranded job. |
| D. LangGraph decision integration | 4–6 days | Refactor graph state to metadata and artifact references; split route and audit decisions from OCR execution; enforce bounded transitions; make Temporal dispatch the selected activity; route `needs_review` to Temporal instead of a separate graph pause. | Digital, good scan, weak scan, fallback, and review fixtures take expected branches; graph never stores page image arrays in persisted state. |
| E. VLM-assisted label drafting | 3–5 days | Add a reproducible local/offline draft generator for the selected scanned corpus, schema validation, hashes, prompt/model provenance, and a separate reviewer artifact. Keep raw draft apart from frozen ground truth. | Reviewer can verify every page and association against source images; evaluator rejects unreviewed drafts. Human review time is additional. |
| F. Runtime VLM challenger in test lane | 4–7 days | Select an approved model and inference boundary; implement adapter, timeout, GPU/CPU capacity reservation, page-image reference input, structured candidate output, disagreement reasons, and fail-closed review routing. Feature stays off in the supported API. | OCR-only and OCR+VLM runs compare on a frozen holdout; no silent field overwrite or cross-tenant artifact access; resource use stays within measured limits. |
| G. End-to-end validation and documentation | 5–8 days | Real API + Temporal + worker tests, multipage/format coverage, tenant and review authorization, replay/restart/cancel, idempotency, queue saturation, cleanup, observability, and migration/rollback notes. | Existing legacy tests remain green; experimental route passes its own acceptance matrix with evidence and known limits. |

**Code-only estimate for the requested four-component test lane:** A–G = **29–45 engineering days** (about **6–9 working weeks** for one engineer). The legacy + Temporal + LangGraph path with offline VLM labels, but without runtime VLM comparison, is A–E plus G = **25–38 engineering days** (about **5–8 working weeks**). A first local end-to-end prototype after A–D is roughly **17–25 engineering days**; it is not yet a production-ready switch. Independent transcript/association review, owner-set accuracy thresholds, representative GPU soak, image build/scan/SBOM, and production backup/alert/ingress checks are separate release gates and may add calendar time. See `VLM_TEST_SUITE.MD`, `GPU_LOAD_TEST.MD`, and `PRODUCTION_OPERATION.MD`.

## Execution order and validation gates

1. **A then B:** lock contracts and make the legacy and experimental callers share complete, bounded extraction. Run differential tests before changing scheduling.
2. **C after B:** create a test-only Temporal namespace/queue or clearly isolated workflow type. Do not expose an unfinished Temporal engine through `/extract/v1`. Prove durable submission/result reconciliation and GPU worker isolation with real restart/failure tests.
3. **D after the C contract exists:** make LangGraph choose and audit work while Temporal performs dispatch and owns the lifecycle. Keep decision loops finite (for example, initial extraction plus at most one configured fallback/challenge pass).
4. **E can start after A:** use a VLM only to draft labels; an independent reviewer corrects them from images and the business owner sets thresholds **before** scoring. Evaluate the existing legacy path and experimental path against the same frozen holdout.
5. **F in the isolated test lane:** benchmark whether a runtime VLM actually improves verified field association/quality enough to justify its cost and latency. A failed experiment leaves the OCR-only Temporal/LangGraph design intact and prevents promotion of the VLM branch.
6. **G closes the code track:** compare outputs and failure behavior side by side; retain the legacy route as the fallback until a separate release decision. The user has asked not to build or deploy, so the GPU image and production operations gates remain plans rather than steps in this code track.

## Acceptance evidence for an eventual route switch

- **Correctness:** complete page accounting and preserved source order; no silent parser success on corrupt or truncated input; explicit `partial`, `needs_review`, and failed states. Compare text, tables, and label/row/column-to-value associations against independent labels, using owner-approved thresholds.
- **Durability:** an API crash after job admission, a Temporal/worker restart, repeated submit, and activity retries each yield one coherent job/result. Review survives restart and applies once to the intended result version.
- **Security:** every artifact and result is scoped by verified tenant/owner; no public route accepts a worker-local path, Temporal server address, namespace, callback destination, or arbitrary model endpoint. Logs and workflow history do not carry document contents.
- **Capacity:** a saturated OCR/VLM GPU queue produces controlled backpressure; CPU polling remains responsive; hard worker limits and artifact retention hold during a representative multi-file/multipage soak.
- **Compatibility:** legacy output remains usable by existing clients, or a versioned response/migration is documented. A rollback can direct new jobs to legacy without orphaning already-running Temporal jobs.

## Decisions that affect the calendar

- **Required now:** confirm whether the initial objective is a test-only Temporal/LangGraph path or eventual replacement of the single-host queue. This plan assumes test-only first. Confirm the representative formats and whether the five RFQ scans are enough for a *prototype* corpus; they are not evidence of broad production accuracy.
- **Before runtime VLM:** choose local versus approved hosted inference, model/version, permitted document handling, GPU budget, and the fields where the challenger is allowed to trigger review. A local model that competes for the OCR GPU may need a second device or strict scheduling; this is measured, not assumed.
- **Before distributed rollout:** choose shared storage, transactional job store, Temporal hosting, retention, and operator ownership. These belong to a later deployment decision. The code-only work above can proceed on a single host without pretending that local SQLite and local files are distributed infrastructure.
