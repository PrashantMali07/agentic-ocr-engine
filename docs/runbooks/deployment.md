# Deployment readiness and operator handoff

The supported code path is FastAPI `POST /extract/v1` with the `legacy` engine, a separate worker, and a SQLite job store. It requires one host and one shared, durable POSIX filesystem for API, worker, inputs, outputs, and `jobs.db`. SQLite on an Azure Blob mount or independent container filesystems is **not** an approved topology. Do not scale API/worker replicas across hosts without replacing the job store and validating distributed ownership. LangGraph and Temporal are not production paths; callbacks and server-directory routes are disabled.

No production deployment, image scan, GPU image smoke, external alert configuration, or encrypted backup/restore drill is recorded for this code. The [Phase 6 evidence runbook](phase06-release-evidence.md) lists release gates. This document is an operator checklist, not a claim that they passed.

## Before release

1. Choose a single-host topology with a durable local filesystem and one worker OCR child. Confirm the GPU host's driver/container runtime can see the selected GPU. Run the verified image offline against `/health`, `/ready`, and representative OCR before connecting ingress. The GPU image has not yet passed that check.
2. Set `INGESTION_API_KEY_SHA256_JSON` from a secret store. Each digest maps to a tenant, subject, and roles. Use separate review/admin credentials as needed. Configure authenticated private TLS ingress; verify unauthenticated, cross-tenant, and stale-review requests fail.
3. Set `INGESTION_OUTPUT_DIR` to the same durable location for API and worker. Keep `OCR_MODEL_MANIFEST` and `OCR_MODEL_ROOT` pointing to the verified, read-only model bundle. Keep `ENABLE_SYNC_EXTRACTION=false` and `ENABLE_LANGGRAPH_ENGINE=false`. Do not start the older Temporal worker.
4. Keep the default admission settings until representative target-host measurements support changes. The six `INGESTION_MAX_*` settings in [.env.example](../../.env.example) are bounded at startup. One OCR child runs at a time. The worker supervises a 20-minute deadline and permits one retry.
5. Set `ENCRYPTED_BACKUPS=true` only when an **actual** encrypted SQLite backup process, restricted storage access, retention policy, and successful restore drill exist. The flag neither creates nor encrypts a backup. Back up SQLite with its online backup API or a transactionally consistent snapshot; copying only a live `jobs.db` file can miss WAL data. Treat backups as containing extracted text and review history.
6. Connect `/operations/metrics` and worker logs to external alerting. Verify delivery for worker heartbeat loss, queue/review age, retries/failures, disk pressure, cleanup failures, and resource peaks. See [retention and monitoring](retention-and-monitoring.md). Keep the worker running for cleanup and readiness.
7. Preserve the current database and image for rollback. Test the schema migration on a copy containing legacy rows. Drain or safely pause admissions before replacing a running worker; verify retained jobs and leases after restart. A rollback must keep disabled routes disabled.

## Code-level behavior to verify

`/health` is liveness. `/ready` checks SQLite access, a recent worker heartbeat, and at least 256 MiB free disk. `/operations/metrics` requires the admin role. Each uploaded PDF is limited to 50 pages; uploads are limited to eight files, 15 MiB per file, and 30 MiB total. Invalid/unsupported documents fail with stable error codes; partly readable PDFs can finish as `partial`; reviewable results use `waiting_for_review`. Finished jobs expire after 24 hours; unreviewed results after seven days. The worker performs retention.

The checked-in Dockerfiles pin base-image digests, package locks, and model hashes, but OS packages still come from mutable distro repositories. The CPU image previously passed a local smoke test; the GPU image has not passed export/container smoke. Image scans, SBOM, model publisher provenance, target-host load, and owner-approved accuracy thresholds remain release work. Do not equate source checks with operational acceptance.
