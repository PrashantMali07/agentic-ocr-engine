# Phase 0 and 1 implementation status

18 October 2026. Code and deployment definitions were changed locally; no cloud resources were deployed or reconfigured.

## Completed

- Container applications run as `appuser` with only output/scratch/model-cache locations writable; source and installed packages are no longer recursively handed to the runtime user. Compose binds to loopback. The Azure deployment script sets internal ingress for new apps, closes any inherited public ingress before an update, and verifies the final setting.
- The remote build context excludes environment files, notebooks, benchmark artifacts, test documents and local generated files. The CPU requirements explicitly include the modules used by the API and native parsing path.
- All document routes require a configured bearer API key. The configuration stores SHA-256 digests of 32-byte-or-longer random keys and maps each key to a tenant, subject, and roles. No mapping means the service fails closed with 503. `/health` remains public and minimal; interactive API documentation endpoints are disabled.
- `/extract/v1` is the only enabled HTTP processing path. The old synchronous extraction and validation routes are disabled by default; directory and batch-directory HTTP routes always return 503; callbacks and the unfinished Temporal API engine are rejected. Legacy extraction is the default; LangGraph requires explicit opt-in.
- All enabled upload paths use one storage helper: validate filenames and extensions, reject duplicate display names, generate random storage names, create files exclusively with no-follow protection, count bytes while copying, check preliminary file signatures, and clean partial uploads on error. A raw-body ASGI middleware caps multipart data before the framework can spool an excessive body. Defaults: eight files, 15 MiB each, 30 MiB total plus 256 KiB multipart overhead.
- The interim in-process queue admits at most eight pending jobs, with two running. This is not a durable or cross-replica control; Phase 2 replaces it.
- Job records now carry tenant and owner fields. Existing SQLite databases are migrated in place; historical rows have null ownership and are unavailable through tenant-scoped API reads. Reviewers can read jobs in their tenant; extractors can read only their own. The review mutation route is disabled pending a durable review workflow.
- Job and graph execution IDs use cryptographic randomness and are separate from client correlation IDs. A paused graph or missing final result fails instead of producing an empty successful extraction. Graph and report HTML tables are sanitized, generated cells are escaped, embedded report JSON is safe for an HTML script context, scalar dashboard cells use `textContent`, and spreadsheet exports neutralize formula prefixes without changing stored values. Routine recovered OCR token logging was removed.

## Operational configuration

`INGESTION_API_KEY_SHA256_JSON` must be set to a JSON object:

```json
{
  "<sha256-of-random-key>": {
    "tenant_id": "indolog",
    "subject": "backend-service",
    "roles": ["extract"]
  }
}
```

Use a separate credential for `review`. The calling backend sends the original key in `Authorization: Bearer <key>`. Keep the key in a secret store; use the digest mapping as a secret-backed application setting. The supplied `.env.example` intentionally has no valid key. The Azure script does not create or rotate the credential. Provision it for the app before routing authenticated backend traffic.

The feature switches `ENABLE_SYNC_EXTRACTION` and `ENABLE_LANGGRAPH_ENGINE` are false by default. They are for controlled compatibility checks, not a production approval. Server-directory, callback, Temporal API and review flows remain disabled even if a client supplies their former parameters.

## Verification

- Twelve focused security/API regression tests passed, including multipart filename escapes, format mismatch, duplicate names, owner and tenant isolation, legacy database migration, raw-body limits with and without `Content-Length`, disabled routes, HTML script injection and CSV formula handling.
- The checked-in OpenAPI JSON and YAML were regenerated from the current routes, document bearer authentication, and default route availability.
- `compileall` passed for package and security tests. Both Compose files passed `docker compose config --quiet`; the Azure PowerShell script passed the PowerShell parser.
- An actual OCR model run, Docker image build, Azure deployment, GPU stress test and production ingress verification were not performed. The local sandbox hung on even a minimal `FastAPI TestClient` request; the tests were run successfully in the host environment after isolating that sandbox behavior.

## Remaining production blockers

Phases 2–6 in `PRODUCTION_HARDENING_PLAN.md` remain. The current queue and job tasks live in API process memory; admission is local to one process. Native parsing is not yet isolated by a killable worker/deadline. LangGraph checkpoints can retain document arrays when explicitly enabled. OCR parser limits, complete scanned-PDF handling, durable human review, retention, secure callbacks, dependency/image hardening and full release benchmarks are still required before accepting untrusted production traffic.
