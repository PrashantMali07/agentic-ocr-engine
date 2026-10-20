# Phased implementation plan for production OCR

Based on `SECURITY_REVIEW.md`, 18 October 2026.

Implementation update: Phase 0 containment and the immediate Phase 1 boundary repairs have been applied locally; see `PHASE_0_1_IMPLEMENTATION.md`. The Phase 2 single-host queue and supervised worker are documented in `PHASE_2_IMPLEMENTATION.md`. The distributed Temporal target and later gates remain planned.

## Target design and scope

Keep FastAPI, PaddleOCR, native format parsers, and the existing Temporal integration. Make Temporal the single production job coordinator instead of maintaining independent `asyncio.create_task` and Temporal queues. Extract shared document/page processing functions so the graph, CLI, and Temporal activities cannot drift into different parser behavior.

The API authenticates callers, validates and stores bounded uploads, submits durable jobs, and returns status/results. CPU parser workers and a dedicated GPU worker execute jobs. A job contains tenant/owner identity, server-generated document IDs, storage references, explicit status, and review history. Document originals and intermediate images stay in bounded storage; workflow/checkpoint payloads carry references, not image arrays or full OCR histories.

Temporal owns production pause/resume and retries. LangGraph may remain an internal extraction graph, but should not maintain a separate production review lifecycle or global in-memory document history. Production extraction returns `needs_review` with an audit payload; the Temporal workflow waits for an authorized review signal.

SQLite can remain for a verified single-host deployment. Multiple API replicas or independently hosted workers require shared durable document storage and a shared transactional job store; use a repository interface so PostgreSQL can replace SQLite without changing route logic. Do not deploy multiple replicas over independent SQLite databases or local upload directories.

## Phase 0 — Contain exposure and establish the deployment contract

**Outcome:** the current vulnerable service is reachable only through an explicitly controlled boundary while repairs proceed. This is containment, not production approval.

Changes:

- `scripts/azure-deploy-ocr.ps1`: default to internal ingress; explicitly configure and verify the intended ingress/authentication settings for both create and update paths. An update must not silently preserve an old public endpoint. Public exposure requires a deliberately configured authenticated gateway path.
- `docker-compose.yml`, `docker-compose.cpu.yml`: bind development ports to loopback by default. Document the explicit override for approved network access.
- `api.py`, `config.py`: disable server-directory HTTP routes, Temporal submission, callbacks, and synchronous OCR routes by default until their respective gates pass. Return a clear unavailable-feature response rather than silently falling back to another engine.
- `.dockerignore`: exclude `.env*` except an intentional sanitized example, notebooks, test documents, benchmark databases/reports, and local generated artifacts. Prefer an allowlisted remote-build context.
- `Dockerfile`, `Dockerfile.gpu`: keep source and the virtual environment root-owned; grant `appuser` write access only to explicit output, scratch, and required cache directories. Precreate those directories with restrictive permissions.
- Document the trusted caller model: recommended default is an authenticated backend calling a private OCR service. If multiple tenants are served, tenant identity must come from verified identity/claims, never arbitrary form fields or client headers.

**Exit checks:** unauthenticated external access cannot reach extraction; disabled routes cannot be used as bypasses; runtime user cannot modify code/dependencies; the ACR context excludes environment files and document data.

## Phase 1 — Repair input, identity, and output boundaries

**Outcome:** requests cannot escape storage boundaries, cross tenant boundaries, or inject executable report content. Covers F01–F03, F07, F08 and immediate F09 containment.

Changes:

- Add `security/auth.py`: use a fail-closed service credential contract that produces a typed principal with subject, tenant, and roles. The implementation uses SHA-256 digests of high-entropy API keys in secret-backed configuration. If migrating to an identity provider, validate issuer, audience, expiry, signature, and allowed algorithms; accept platform identity headers only through an enforced trusted boundary. Protect every extraction/read/review route; keep public health output minimal.
- Add proposed `storage/uploads.py`: one upload implementation for all routes. Allocate a random server document ID and generated filename; retain the original filename only as metadata. Reject absolute/traversal/separator-bearing names, invalid names, and duplicate display names under the current filename-keyed response contract. Use exclusive, no-follow creation within a job directory and clean up partially written uploads on errors/disconnects.
- Add byte-counting request admission before multipart parsing; do not rely only on `Content-Length` or a check after `UploadFile` has already spooled. Enforce aggregate request bytes, file count, per-file bytes, allowed format, and appropriate signature/container validation. Text formats need bounded encoding/content checks rather than a fictional universal magic signature.
- `api.py`: use the shared uploader everywhere; reject unknown engines with an enum. Keep `/extract-dir` and `/batch-extract` disabled for ordinary clients. If retained for administrators, accept tenant-scoped relative paths resolved beneath a configured root, reject symlinks/escapes, and cap discovery work. Keep unrestricted local CLI use separate from HTTP authorization.
- `config.py`: replace timestamp-plus-`random.getrandbits` identity generation with cryptographically generated IDs. Separate client correlation IDs from internal job/document/execution IDs.
- `job_store.py` and versioned database migrations: add `tenant_id`, `owner_id`, per-document IDs, and review actor metadata. Scope every read/update by tenant and permissions. Quarantine historical rows lacking ownership rather than assigning them to whichever caller requests them.
- `graph/workflow.py`: stop deriving execution identity from client request IDs or filename stems; initialize fresh state. Stop turning a missing final result into successful empty output. Return an explicit incomplete/review-required state until the review workflow is implemented.
- Add proposed `core/safe_html.py`; update `graph/table_builder.py`, `extraction/native.py`, `extraction/pdf.py`, and HTML triage: escape generated text and sanitize retained HTML through one strict table-only allowlist. Strip active elements, event attributes, unsafe URLs, and external resources.
- `simulator/report.py`: make embedded JSON safe for the HTML script context, replace scalar `innerHTML` interpolation with DOM/text APIs, and render only sanitized table markup. Protect spreadsheet-oriented CSV exports in `simulator/db.py` without changing the canonical extracted values.

**Exit checks:** real multipart requests cannot write outside temporary storage, overwrite an existing file, exploit symlinks, or bypass byte limits with chunked bodies. Tenant A cannot read/review tenant B's job. Repeated correlation IDs and same-stem files do not share state. Hostile document text remains inert in a browser-rendered report. A paused/empty graph cannot produce completed/high-confidence output.

## Phase 2 — Build one durable, bounded execution path

**Implementation note:** The first Phase 2 code change uses one SQLite-backed queue plus a separate supervised worker on a shared single-host output directory. This removes API-process OCR tasks and makes queued jobs restart-safe without enabling the existing incomplete Temporal workflow. A distributed deployment still requires the shared storage/database and Temporal reconciliation work below.

**Outcome:** all extraction consumes controlled capacity and survives restarts. Covers F04–F06 and Temporal lifecycle defects.

Changes:

- Add proposed `services/jobs.py` and `storage/artifacts.py`: centralize admission, upload reservations, job creation, artifact ownership, submission, cleanup, and cancellation.
- Add atomic limits for pending jobs and pending bytes globally/per tenant, plus rate limits. Reserve capacity before accepting expensive work and release it on terminal failure or cancelled upload. Configure ingress and application limits consistently.
- Make `POST /extract/v1` the canonical submit endpoint. Remove `_background_tasks` as the production scheduler. If synchronous compatibility must remain, it waits asynchronously on the same admitted job with a bounded response deadline; it must never run OCR inline or bypass capacity controls. Version/deprecate the response contract explicitly.
- `temporal/client.py`: fix argument passing using the installed SDK's supported `args` interface; use server-generated idempotent workflow IDs and configured connection settings. Do not let request parameters choose the Temporal server or namespace.
- `temporal/activities.py`, `temporal/worker.py`: run blocking CPU work outside the asyncio event loop, preferably in supervised worker processes. Use a dedicated GPU process with one inference at a time initially, including model initialization; do not create one model per HTTP worker. Keep image arrays inside the processing boundary.
- `api.py`: remove the premature attachment deletion after workflow submission. Activities receive durable storage references accessible to their workers, not an API-container-local path assumed to exist elsewhere.
- `job_store.py`: add guarded state transitions, version/attempt fields and atomic claims. Add durable submission reconciliation/outbox behavior: a crash between writing a job and starting its workflow must not leave it permanently stranded or duplicated.
- `temporal/workflows.py`: persist/bridge progress and terminal results back into the job store. Implement idempotent retries, cancellation and timeout states. A callback delivery failure must not restart OCR.
- Enforce CPU, memory, scratch-space and wall-clock budgets in the worker supervisor/container runtime. Temporal activity timeouts and cancelling `to_thread` do not kill native computation; terminate/recycle the relevant worker when a hard deadline is exceeded.
- Remove global `MemorySaver` from normal production runs. Pending human review will be durable job/workflow state, not an unlimited history of image arrays. Bound intermediate artifacts and result payloads.

**Exit checks:** saturation produces controlled rejection; health/polling stays responsive during long OCR; queued work survives API/worker restart; retries do not duplicate results; cancelled/timed-out native work releases capacity; inputs remain available until authorized cleanup; sequential completed jobs do not grow retained memory indefinitely.

## Phase 3 — Make document processing complete and bounded

**Outcome:** supported formats have explicit routing, full page coverage, and predictable resource costs.

Changes:

- Add proposed `processing/preflight.py`: bounded format validation and limits for decoded image pixels, image frames, PDF page count, archive members/expanded size/compression ratio, spreadsheet rows/columns/cells, and output bytes. Header inspection is preliminary protection; untrusted parsing still runs in the resource-limited worker.
- Add proposed `extraction/service.py`: one format dispatcher and page iterator used by graph, Temporal, validation, and CLI. Route DOCX/HTML/CSV/XLS/XLSX to their native parsers; reject unsupported engine/format combinations rather than sending Office documents to `cv2.imread`.
- `extraction/pdf.py`, `graph/nodes.py`, `temporal/activities.py`, `extraction/decoupled_pipeline.py`: choose and enforce one rendering contract, including one-based page numbers and a typed return value. Repair the current `page_idx`/missing-`out_dir` mismatch. Process every page in scanned and mixed PDFs, preserving original order and page provenance.
- `extraction/manager.py`: render, process and release bounded page batches rather than retaining all scanned pages before prediction. Close documents/workbooks/images and delete scratch files through context managers/finally blocks.
- `extraction/ocr.py`, `graph/nodes.py`, `extraction/layout_order.py`: apply image budgets before allocating deskew/resize intermediates; prevent pathological aspect ratios from causing oversized upscales.
- `extraction/hybrid.py`: set explicit Tesseract deadlines; preserve original recognized values alongside proposed corrections. Do not silently alter business identifiers or monetary strings without provenance and review policy.
- `extraction/excel.py`, `extraction/csv_parser.py`, `extraction/native.py`: bounded iteration/expansion, consistent encrypted/corrupt/unsupported results, and no conversion of parser failure into apparently successful empty content.
- `schema/responses.py`: expose per-document/per-page status, processed page counts, warnings, and provenance. Distinguish complete success, partial extraction, password-required, review-required, and failure. Missing confidence remains unknown rather than defaulting to `1.0`.

**Exit checks:** a curated fixture suite covers each advertised format, digital/scanned/mixed multi-page PDFs, multi-frame images under the chosen support policy, encrypted/corrupt files, oversize and high-expansion inputs, duplicate filenames, and empty documents. Assert exact page accounting and explicit partial failures. Validate normal OCR quality on a labeled representative corpus before accepting behavior changes.

## Phase 4 — Finish human review, retention, and operational visibility

**Outcome:** results have a trustworthy lifecycle and document data expires consistently. Completes F06 and F09.

Changes:

- `schema/responses.py`, `temporal/workflows.py`, `services/jobs.py`: define and enforce transitions such as `queued → processing → waiting_for_review → completed`, plus `failed`, `cancelled`, `timed_out`, and expiry behavior. No result is final until required review finishes.
- `graph/nodes.py`, `graph/workflow.py`: return an explicit extraction/audit outcome to the coordinator; remove the independent production `interrupt()` lifecycle. If standalone LangGraph interruption remains as a development feature, isolate it from production routes and storage.
- `api.py` review route, `schema/requests.py`: use typed corrections targeted to a specific document and result version. Require reviewer authorization; prevent one correction from overwriting every document; reject conflicting/stale reviews.
- `temporal/client.py`, `temporal/workflows.py`: signal the authorized workflow, apply corrections once, regenerate dependent text/table/HTML representations, and retain the original result plus reviewer identity, timestamp, and change history. Use a durable review-submission record/reconciliation step so signal failures do not lose approved corrections.
- Add proposed `maintenance/retention.py`: scheduled cleanup of job rows, inputs, scratch, results, review state and any checkpoints. Separate retention policies for active, paused, failed and completed jobs. Use leases/terminal-state checks to prevent deleting live inputs; interrupted cleanup must be safe to retry. Document backup and Temporal-history retention separately.
- `job_store.py`: enforce expiry on access and expose consistent not-found/expired behavior without leaking other tenants' identifiers.
- `extraction/hybrid.py`, API and worker logging: remove document tokens and sensitive callback URLs from routine logs. Return stable error codes to clients, retaining redacted diagnostic detail internally.
- Add operational metrics for queue depth/age/bytes, duration, timeout/OOM/retry count, review age, cleanup failure, disk usage and GPU memory. Separate liveness from readiness and dependency health.

**Exit checks:** review survives restart, requires the correct role, resumes exactly once and targets only the selected document. Expired content is inaccessible and eventually removed; active work is never purged. Logs do not contain fixture secrets or document text. Alerts fire for stuck jobs and capacity exhaustion.

## Phase 5 — Secure callbacks and make builds reproducible

**Outcome:** enable optional network delivery only after its security controls exist; make the deployed artifact auditable.

Changes:

- Add proposed `delivery/webhooks.py`: use tenant-owned, pre-registered HTTPS destinations instead of arbitrary per-request URLs. Reject credentials in URLs, disallowed ports, loopback/private/link-local and other non-public addresses unless a deliberate private-destination policy exists. Validate IPv4/IPv6 and DNS results at connection time; prevent DNS rebinding through an enforced egress proxy/network policy. Disable redirects or revalidate every hop.
- Stream/limit callback responses and apply connect/read/total deadlines. Sign payloads with a timestamp and event ID; persist delivery attempts and bounded retry/backoff. Separate extraction state from delivery state and document at-least-once delivery/idempotency for receivers.
- `api.py`, `temporal/activities.py`: both use the same delivery implementation. Repair the missing HTTPX import through that refactor; do not enable the old unrestricted POST path first.
- `pyproject.toml`, `uv.lock`: upgrade urllib3 to a compatible version fixing the reviewed advisories; rerun the advisory check and regression suite. Keep the advisories' reachability caveat in the change description.
- `requirements-cpu.txt`, both Dockerfiles, `scripts/prefetch_models.py`: create reproducible CPU and GPU dependency sets, pin build tools/base images by reviewed versions/digests, and lock the model-download environment. Smoke-test both images; explicitly declare imports needed at runtime rather than relying accidentally on transitive dependencies.
- `extraction/layout_order.py`, model initialization: pin model revisions/artifact checksums; prefetch all production-selected models; use read-only caches and disable unexpected runtime downloads. Fail readiness when required artifacts are missing.
- Deployment scripts: prefer managed identity for registry access, restrict worker egress, apply supported read-only-root-filesystem/resource controls, and configure encrypted transport/storage according to the actual hosting boundary.

**Exit checks:** callbacks cannot access forbidden destinations through literal IPs, IPv6, DNS changes or redirects; retries are bounded and independently observable; signed deliveries can be verified. Both images start with verified model artifacts and no download dependency. Dependency and image scans have no unresolved release-blocking findings.

## Phase 6 — Prove release readiness and roll out

**Outcome:** a controlled production release backed by executable evidence.

Changes:

- Add dedicated `tests/security/`, `tests/integration/` and bounded load/soak tests. Keep unit probes with mocked inference, but add real ASGI multipart, real parser, Temporal restart/cancellation, browser output-safety and GPU serialization tests. Mock-only tests cannot be the release gate.
- CI: run dependency/secret/static checks and the CPU suite on changes; build and scan release images, emit an SBOM, and use a GPU runner/staging gate for the selected engine. Test database migrations against a copy containing legacy rows.
- Update `README.md`, `DEPLOYMENT.md`, `TEMPORAL_GUIDE.md`, API schemas and runbooks to match actual auth, statuses, limits, supported formats, review, retry and retention behavior.
- Measure throughput, p95/p99 latency, peak RAM/VRAM/scratch use and OCR accuracy on representative documents. Derive production admission limits from measured capacity; put configurable ceilings in place before tuning them. Define acceptance thresholds before the staged load run.
- Start with one GPU worker and a verified deployment/storage topology. Canary authenticated traffic, observe error/quality/capacity metrics, and expand only after the gates pass. Prepare database-compatible rollback and queue draining; rollback must not reopen insecure routes or reactivate discarded job ownership assumptions.

**Release gate:** all prior phase exit checks pass; no cross-tenant access, path escapes, unsafe HTML, hidden review states, unexplained page loss, uncontrolled resource growth, or stranded jobs in the bounded test suite. Verify the deployed ingress and image configuration directly; documentation alone is insufficient.

## Implementation ordering and compatibility decisions

Execute Phases 0–4 in order. Phase 1 HTML fixes and Phase 5 dependency/build fixes can land early as isolated changes; callbacks stay disabled until Phase 5 controls pass. Phase 6 tests should be added alongside each implementation phase, with the final end-to-end gate at release.

Decisions to resolve before their dependent work: trusted token issuer/tenant mapping (Phase 1); single-host versus shared storage/database topology (Phase 2); compatibility period for synchronous clients (Phase 2); review roles and retention policy (Phase 4); approved callback destinations (Phase 5). These do not block immediate containment or file-handling fixes.

Recommended reviewable change sets: deployment containment; shared secure uploads; auth/ownership migration; safe report rendering and execution identity; bounded durable dispatch; supervised workers/storage lifecycle; format/page correctness; review and retention; secure delivery; reproducible builds; release integration and documentation. Every change set includes the relevant regression checks and migration/rollback notes.

Production approval requires the applicable gates, not merely completion of the initial hotfix. Keep callbacks, directory administration, or an engine disabled if its gate has not passed.
