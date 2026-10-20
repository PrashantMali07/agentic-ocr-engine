# Phase 02 build spec — isolated Temporal job lane

Estimate: **6–9 engineer-days** after Phase 01. The supported SQLite worker continues to own legacy jobs. Temporal owns only jobs explicitly admitted to the experimental lane.

## Submission and state contract

- In `api.py` and `job_store.py`, use a server-side scheduler marker (`legacy` or `temporal_test`) and a versioned job request. Ordinary callers cannot choose a Temporal server/namespace, local file path, or worker queue. Keep the experimental API/CLI boundary disabled for general traffic until validated.
- Atomically record admitted jobs and a submission intent/outbox in SQLite. A reconciler starts an idempotently named Temporal workflow and records its workflow ID. If API or reconciler crashes between these operations, retry must start or find the same workflow, not create a duplicate or strand the job.
- Repair `temporal/client.py` to call the installed SDK with a supported argument form (`args=[...]` or one typed request) and configured connection settings. Workflow ID derives from server job ID, not source filename.
- Refactor `temporal/workflows.py` to receive small `JobRequestV1` metadata, enumerate a bounded page manifest, dispatch CPU and GPU activities by reference, aggregate ordered page results, and reflect progress/terminal status in the job store. Workflow history must not contain full OCR text, images, tables, or raw VLM output.
- Refactor `temporal/activities.py` and `worker.py` for idempotent artifact writes, process-safe blocking work, heartbeats where needed, hard deadlines, and cleanup. Use one GPU OCR worker process in the local test lane; `max_concurrent_activities=1` is per worker and does not by itself enforce a global limit.
- Review requests remain authorized in FastAPI. Persist a versioned review event, signal the workflow with the event ID, and make both sides idempotent. Temporal alone waits/resumes; a signal failure is reconciled without losing an approved review.

Likely edit points: `api.py`, `job_store.py`, `services/worker.py`, `temporal/client.py`, `workflows.py`, `activities.py`, `worker.py`, `schema/requests.py`, `schema/responses.py`, and a new submission reconciler.

## Failure semantics and acceptance

Define explicit retryable versus permanent errors. Timeouts/cancellation of native OCR require a supervised process that can be terminated or recycled; cancelling an asyncio task is not sufficient. Activity output is committed under a deterministic `(job, page, stage, policy-version)` key with checksum so a retry reuses or safely replaces only its own result. The legacy worker query excludes Temporal-marked jobs. Integration tests with a real Temporal test server cover admission crash, duplicate submission, worker/server restart, retry, cancellation, review wait/resume, GPU queue saturation, and tenant-scoped result reads. No experiment job remains indefinitely queued without a diagnosable reason.
