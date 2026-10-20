# Production OCR security review

Date: 18 October 2026

Implementation note: This records the original findings. Phases 0 and 1 were subsequently applied locally; current status and remaining production blockers are in `PHASE_0_1_IMPLEMENTATION.md`.

**Assessment: not ready to accept untrusted production documents or expose the supplied API publicly.** The highest-priority issues are arbitrary file overwrite, unrestricted server-document access, missing authorization, and resource exhaustion. An authenticated private network reduces exposure but does not repair these boundaries.

This is a repository-wide static security review with targeted local validation, not a penetration test or certification. Application source, API/schema, parsers, graph and Temporal orchestration, storage, reporting, Docker/Compose, deployment scripts, dependency manifests and relevant documentation were examined. A syntax inventory covered 61 Python files; 15 notebook code cells received a dangerous-operation scan. Input documents, notebook outputs, stored business records, Git history, cloud configuration, built container images, and third-party native implementation code were not audited. No production code or configuration was changed.

## Priority findings

### F01 — Critical: uploaded filenames permit arbitrary file overwrite

**Evidence:** `src/ingestion_pipeline/api.py:302-305`, `369-372`, `494-497`. All three upload handlers join `attachments_dir / file.filename`, then open that path with `wb`. There is no filename validation or containment check before writing. Absolute filenames discard the directory prefix; `../` components escape it. This occurs before extension checks or OCR.

Any caller able to reach `/extract`, `/extract/v1`, or `/validate-attachments` can create or truncate files writable by the service account, provided parent directories exist. A later parsing error does not undo the write. Both Dockerfiles recursively give the runtime user ownership of `/app`, including application source and its virtual environment (`Dockerfile:59`, `Dockerfile.gpu:105`). Consequently, overwrite can target code/configuration and may become code execution on a later import or restart. Code execution was not attempted or demonstrated.

**Validation:** executed the actual `/extract` handler function extracted from its AST, replacing the downstream parser with a stub. An absolute-path filename overwrote a synthetic marker in a temporary directory outside the upload directory.

**Fix:** generate server-side storage names; retain original names only as metadata. Validate extensions/signatures before dispatch, enforce containment and exclusive/no-follow file creation, and keep application code and dependencies root-owned/read-only. Reject duplicate document names or give documents separate identifiers.

### F02 — High: API callers can read arbitrary supported documents on the server

**Evidence:** `api.py:452`, `598`; `extraction/manager.py:49-77`.

`/extract-dir` and `/batch-extract` pass client-supplied paths directly to `collect_input_files`. Despite its directory label, the parameter also accepts individual files. Recursive directory discovery has no configured root, tenant boundary, or symlink containment check. Parsed content is returned to the caller. This exposes service-readable PDF, Office, image, CSV, and HTML documents, including other jobs' uploads and mounted business data. This is supported-document disclosure, not proof that arbitrary binary files or `/etc/shadow` can be returned.

**Validation:** the actual collector accepted an absolute synthetic HTML file outside any approved input root.

**Fix:** remove these routes from the public API or restrict them to an authenticated administrative role. Resolve paths against an explicit tenant-scoped root, reject escapes and symlinks, and authorize each document independently.

### F03 — High: missing authentication and job-level authorization conflict with external ingress

**Evidence:** `api.py:60`, `391-430`; `job_store.py:31-36`; `scripts/azure-deploy-ocr.ps1:69`; Compose publishes `8000:8000`.

No route dependency or middleware authenticates callers. Job records have no owner/tenant field. Anyone with a job ID can fetch extracted content or submit corrections, and no reviewer role or audit identity is required. UUID-like identifiers are not an authorization boundary. The custom job identifier additionally uses a timestamp plus 32 bits from `random.getrandbits`, which is unsuitable as a bearer secret.

Documentation says platform/network authentication protects the service (`docs/services/ingestion-pipeline.md:23`), but the supplied create script enables external ingress without configuring authentication or network restrictions. Existing deployed controls may differ; they were not inspected.

**Fix:** enforce service authentication and restrictive ingress in deployment, with a bypass-resistant trust boundary. Persist owner/tenant identity and require it on every job read/update. Require a reviewer role and immutable review audit trail. Use cryptographically generated IDs, without treating them as substitutes for authorization.

### F04 — High: upload and queued-work admission are unbounded

**Evidence:** `api.py:364-378`, `164-174`, `186`; `extraction/manager.py:169-183`; `extraction/csv_parser.py:52-56`; `extraction/excel.py:81-101`; `graph/nodes.py:122-160`.

The service copies uploaded files without an application byte budget, saves them before acquiring its two-job semaphore, and creates an unrestricted number of waiting tasks. The semaphore caps running jobs only. There is no aggregate pending-byte budget, bounded queue, per-caller quota, document page budget, or enforced overall processing deadline. Framework multipart defaults do not supply those controls.

Parser amplification compounds this: legacy PDF processing renders and retains every scanned page before batching; CSV parsing loads all rows; Office archives have no application-level expanded-size budget; images are decoded before any legacy resize, and the default graph has no maximum-size downscale before deskewing. `OCR_MAX_DIM` is not a complete decompression/resource boundary. Tesseract calls omit its timeout option (`extraction/hybrid.py:131`, `190`). Repeated or oversized documents can exhaust disk, memory, CPU, or GPU time.

**Fix:** enforce streaming request/file limits at both ingress and application, pending-byte and pending-job caps, tenant quotas, and explicit overload rejection. Bound decoded pixels, pages, rows/cells, archive expansion, output size, and processing time. Process PDF pages incrementally. Run untrusted native parsing in disposable workers with enforced memory/CPU/time limits; cancelling a Python thread is not a hard parser timeout.

### F05 — High: synchronous OCR blocks the API event loop

**Evidence:** `api.py:315`, `335`, `459`, `503`, `604`.

The synchronous extraction, directory, validation, and batch routes are `async def` handlers but call expensive synchronous parsing/OCR directly. They bypass `_job_semaphore`. A single slow input can occupy the sole Uvicorn worker's event loop, preventing normal polling, uploads, and health responses from being dispatched. Background inference and synchronous inference can also overlap outside the intended admission policy.

**Fix:** route all engines/endpoints through the same bounded worker queue. Keep the HTTP event loop free of parsing and long file-copy operations. Prefer killable worker processes with deadlines to merely adding `asyncio.to_thread`.

### F06 — High: document checkpoints accumulate indefinitely in process memory

**Evidence:** `graph/workflow.py:103-118`; `graph/state.py`; `graph/nodes.py:140`, `160`; `job_store.py:26-29`, `97-105`.

The singleton graph owns a `MemorySaver` without expiry or deletion. States include original/preprocessed image arrays, OCR text, tables, and results. New request IDs create new retained checkpoint histories, so memory use grows even with sequential requests and after attachment directories are deleted. This is both a memory-exhaustion path and retention of sensitive document content. SQLite's advertised 24-hour TTL is also not scheduled anywhere; `get()` returns expired rows, and completed output directories are not purged by the row-deletion helper.

**Validation:** the installed LangGraph retained multiple checkpoints after completed invocations in a small synthetic graph using the same saver/configuration pattern. No large-memory stress test was run.

**Fix:** explicitly delete completed checkpoint histories, bound paused-review retention, and avoid serializing full images through every graph checkpoint. Implement a scheduled retention policy covering database rows, checkpoints, uploads, scratch files, visualizations, and backups.

### F07 — High: client-selected checkpoint IDs allow document-state contamination

**Evidence:** `api.py:315`; `graph/workflow.py:131-138`; `graph/nodes.py:66-88`, `101-118`, `475-497`.

The synchronous API builds the graph thread key from the supplied `request_id` and filename stem. The singleton saver shares this namespace across callers. A new invocation supplies only `file_path`; LangGraph carries forward unspecified state from an existing thread. Digital branches do not clear prior tables, raw OCR fields, or human corrections, and finalize exports several of those fields. Reusing an ID/stem can return stale document data or corrupt the current result. Same-stem files with different extensions can collide within one request. Cross-caller exploitation requires knowing/reusing the prior key in the same process; it does not require recovering the random async job ID when callers use predictable synchronous request IDs.

**Validation:** installed LangGraph behavior was reproduced with synthetic documents: a second invocation on the same thread retained the first document's tables. This was a focused state-semantics test, not a full GPU-based cross-tenant exploit.

**Fix:** allocate unique server-controlled, tenant-scoped document execution IDs. Separate new-run and authorized-resume operations, fully initialize new document state, and delete history after completion.

### F08 — High: document content can execute script in generated benchmark reports

**Evidence:** `simulator/report.py:46-47`, `236-238`, `257-260`, `293`; `graph/nodes.py:107-111`; `graph/table_builder.py:184-189`; `extraction/native.py:75`, `121`; `extraction/pdf.py:93`.

The report embeds raw `json.dumps` output directly inside a `<script>` block. JSON escaping does not escape the HTML `</script>` terminator, so document text can break out and introduce a new script. The dashboard also assigns document-derived filenames and table HTML to `innerHTML`. Uploaded HTML tables retain active elements/attributes, while OCR/DOCX/PDF table builders interpolate cell text without HTML escaping.

The benchmark dashboard provides a concrete browser sink when an operator opens a report containing untrusted documents. API JSON alone does not execute JavaScript; other consumers become vulnerable if they render these fields as trusted HTML. Impact depends on the report's origin and what data is embedded in it.

**Validation:** the actual report generator preserved a harmless synthetic `</script><script>window.REVIEW_MARKER=1</script>` payload verbatim. Generated markup was inspected; no payload was executed in a browser.

**Fix:** encode embedded JSON safely for HTML, use `textContent` for scalar fields, escape all generated table cells, and sanitize preserved HTML through a strict table-only allowlist. Use a restrictive CSP and isolated report origin as additional protection.

### F09 — High integrity risk: interrupted human-review workflows are presented as completed

**Evidence:** `graph/nodes.py:441-466`; `graph/workflow.py:137-138`; `api.py:197-215`, `241-247`, `405-430`.

`interrupt()` returns a paused graph state without `final_result` on a fresh execution. The wrapper converts that to `{}`. API code then manufactures empty content with default confidence `1.0` and records the job as completed. The review endpoint edits the stored response but never resumes the graph with `Command(resume=...)`. Required review can therefore disappear from the API result while downstream systems receive apparent success. With a reused checkpoint, an old final result can make this still more misleading.

**Validation:** installed LangGraph returned an interrupt and no final result in a synthetic review-gate test. The API's empty-result/default-success path is directly visible in source.

**Fix:** represent `waiting_for_review` explicitly, return no completed result until finalization, preserve the review context, and resume the authorized graph execution after validated corrections. Treat missing final results as incomplete/error, not confident success.

### F10 — Medium: locked dependency has three published security advisories

**Implementation update (19 October 2026):** The original finding below describes the reviewed lock at that time. `uv.lock` now resolves `urllib3==2.8.0`, and both dependency definitions require `>=2.8.0,<3`; see `PHASE_5_PROGRESS.md`. The three listed version-based advisories are addressed in source dependencies. A fresh whole-image advisory scan and download/proxy regression checks remain outstanding.

**Evidence:** `uv.lock:2900-2901` pins `urllib3==2.7.0`.

OSV was queried for all 171 registry package/version entries in the lock. It returned six advisory records for urllib3, deduplicating to three vulnerabilities; no other queried entry produced a match. This checks lockfile versions against available advisories, not runtime reachability or native/container dependencies.

| Advisory | Condition/impact | Fixed version |
| --- | --- | --- |
| [CVE-2026-97687 / GHSA-8988-9cw3-xx77](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77) | Certain HTTPS-proxy TLS settings can be ignored or mixed with target settings | 2.8.0 |
| [CVE-2026-97688 / GHSA-gh4c-6fx4-qh6g](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g) | Crafted chunked Deflate streaming response can cause an infinite CPU loop | 2.8.0 |
| [CVE-2026-97689 / GHSA-vxq7-64xx-v4gw](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw) | Streaming a malicious chunk-size line can consume unbounded memory | 2.8.0 |

**Fix:** resolve to a compatible urllib3 2.8.0 or later, regenerate the lock, and verify downloads/proxy behavior. Reachability through transitive model-download clients was not established. The webhook uses HTTPX, so these advisories should not be attributed to that HTTP client.

### F11 — Medium: remote build context can include environment files and document artifacts

**Evidence:** `.dockerignore`; `scripts/azure-build-ocr-gpu.ps1:28-35`.

The ACR script uploads the project context. `.dockerignore` excludes `blob/` and `output/`, but not `.env`, `test_asset/`, `benchmark_results.db`, `benchmark_reports/`, or notebooks. Those paths exist in this checkout and can contain configuration or document content. Dockerfiles copy selected files, so this is build-context transfer/retention risk, not a claim that these files are present in the final image. No secret value or sensitive document content was inspected or reproduced to prove leakage.

**Fix:** construct an allowlisted build context, or exclude environment files, datasets, benchmark stores/reports, notebooks, and local artifacts. Use build secrets rather than context files when credentials are required.

## Dormant and deployment-dependent risks

**Unrestricted webhook destinations / SSRF:** `temporal/activities.py:240-255` POSTs to arbitrary callback URLs with no destination policy. If enabled, this can reach loopback/private/link-local services and send extracted content to unauthorized destinations. However, the current API webhook path references `httpx` without importing it, and the Temporal start helper currently fails its SDK call signature. Therefore a working end-to-end callback SSRF exploit through the supplied API is **not confirmed**. Repairing those defects without adding destination controls would activate the risk. Use pre-registered HTTPS destinations, explicit address/network validation, DNS-rebinding-resistant connection enforcement, egress restrictions, bounded response handling, and signed callbacks. [OWASP SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).

**Model/build supply chain:** the GPU application install uses a frozen lock, but the CPU image and disposable model-prefetch environment use floating requirements; UV and base image references are not digest-pinned. Optional LayoutReader download fallback (`extraction/layout_order.py:71-73`) has no fixed model revision. Pin and verify production artifacts, prefetch all required models in a controlled build, disable unnecessary runtime downloads, and scan the final image. No malicious model or unsafe remote-code option was found in the reviewed first-party loaders.

**Sensitive logs and CSV exports:** recovered OCR tokens are logged at INFO (`extraction/hybrid.py:299-301`), and callback URLs are logged verbatim, potentially including credentials in URLs. Benchmark CSV exporters preserve document-controlled strings without spreadsheet formula neutralization (`simulator/db.py:290-309`). Restrict/redact content logs and use safe spreadsheet-export handling where operators open untrusted values. These are conditional exposure paths, not demonstrated credential theft.

## Additional production reliability blockers

- Scanned PDF handling calls `render_pdf_page_to_image(..., page_idx=0, dpi=220)` in graph and Temporal triage, but the function requires `page_num` and `out_dir` and returns a filename, not an array. Scanned/mixed PDFs fail this path. Fixing the signature alone leaves first-page-only processing; all pages need explicit coverage and aggregation.
- Temporal `Client.start_workflow` is passed two positional workflow arguments (`temporal/client.py:41-46`), which the installed SDK rejects. A signature-binding test confirmed this. Use the SDK's `args` interface.
- After Temporal submission, `_run_extraction_job` returns and its `finally` deletes attachments (`api.py:221-228`, `275-276`) before asynchronous workers can reliably read them. There is no completion bridge updating the SQLite job. Store durable input references, retain inputs until workflow termination, and reconcile final job state.
- Non-Temporal callback attempts fail because `api.py` never imports `httpx`; callback errors are swallowed/logged while the job stays completed. Delivery requires explicit durable status and retry policy.
- Default pure-Paddle inference uses a cached predictor without the lock used by the legacy engine (`extraction/decoupled_pipeline.py:37-49` versus `extraction/ocr.py:36-44`), while async jobs can run in two threads. Concurrency safety and cold-start initialization need validation; a race or cross-request output leak was not demonstrated.
- Queue tasks exist only in process memory; restart loses pending work while SQLite jobs can remain queued/processing. Add durable dispatch and restart reconciliation.
- Default graph routing supports HTML/PDF/images but treats DOCX/XLSX/CSV as images, although the service advertises them as supported. Route formats explicitly and reject unsupported engine/format combinations.

## Validation and limits

Six safe local checks passed: actual upload-handler overwrite with a synthetic temporary marker; unrestricted collector path acceptance; report script-terminator preservation; installed LangGraph state reuse/history retention; LangGraph interrupt behavior; installed Temporal SDK argument rejection. OCR and model imports were avoided by extracting selected functions from source and mocking downstream processing. The graph tests used small synthetic graphs to validate the library semantics the application relies on. These are not full HTTP/GPU integration tests.

The temporary probe is `/tmp/ocr_security_checks.py`; raw OSV matches are `/tmp/ocr_osv_audit.json`. No live endpoint probing, malicious model loading, actual browser script execution, resource-exhaustion stress test, or cloud changes were performed. A targeted redacted secret-pattern scan found no matches in the files checked; this is not a full secret-history audit. The sandbox mount mechanism failed during the review; approved narrowly scoped commands continued outside it.

Positive controls observed: non-root container runtime, parameterized SQL in job and benchmark stores, a legacy predictor lock, generated async job IDs, and attachment cleanup after normal non-Temporal jobs. These controls do not mitigate the boundary issues above.

## Remediation sequence and acceptance checks

1. Before untrusted access: close/verify ingress authentication, repair file writes, disable or constrain directory routes, enforce owner/reviewer authorization, and remove runtime write permissions from code/dependencies.
2. Establish one bounded execution service for every route, with upload/queue/document budgets and hard worker deadlines. Add checkpoint and storage expiry, unique execution IDs, and restart recovery.
3. Repair HTML/report output, review-state handling, and all scanned-PDF/Temporal lifecycle defects. Add callback destination policy before re-enabling delivery. Upgrade urllib3 and scan the built image.
4. Gate release on tests for traversal/absolute paths/symlinks/duplicate names; unauthorized reads and corrections; oversized and high-expansion documents; queue saturation; cancellation/timeouts; stable memory after sequential jobs; checkpoint isolation; review pause/resume; hostile HTML; full multi-page extraction; and restart recovery.

Upload controls should follow defense in depth across filenames, content, limits, storage, and access control. [OWASP File Upload guidance](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html).
