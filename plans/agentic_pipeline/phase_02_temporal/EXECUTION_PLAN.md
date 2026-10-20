# Phase 02 execution plan — isolated Temporal job lane

1. Add scheduler and workflow fields through an idempotent SQLite migration. Preserve existing rows as `legacy`; quarantine rows whose owner is unknown. Make the legacy claim query explicitly filter on scheduler kind.
2. Implement an admission transaction and outbox/reconciler. Use the Phase 00 request contract and a deterministic workflow ID. Test crash points before and after `start_workflow`.
3. Refactor workflow activities to call the Phase 01 shared service by tenant-scoped artifact ID. Partition bounded pages, enforce per-job/page concurrency, and keep Temporal payloads below the chosen metadata budget.
4. Start CPU and a single GPU worker in the isolated test lane. Prewarm models once per GPU process; set explicit activity deadlines and supervised hard kill/recycle behavior for stalled native work.
5. Bridge workflow transitions to job status/results using guarded, idempotent writes. Add cancellation and failure mapping, plus durable review signal reconciliation.
6. Run real Temporal tests for restart/replay, duplicate start, retries, cancellation, partial pages, and review. Run current legacy tests to prove the two schedulers do not steal one another's jobs.

Deliverables: isolated Temporal submission path, outbox/reconciler, refactored workflow/activities/workers, migration, integration tests, and an operator-facing local test procedure. Exit only when a restarted worker completes admitted work exactly once from the API client's perspective and status polling reflects terminal truth.
