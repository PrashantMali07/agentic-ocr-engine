# Phase 06 execution plan — integration and acceptance

1. Assemble a reproducible local test stack: FastAPI, isolated Temporal test namespace/server, CPU worker, one OCR GPU worker, and bounded VLM worker. Seed only hash-listed permitted fixtures.
2. Run functional and failure-injection scenarios through real API and worker boundaries. Confirm idempotency, restart recovery, review targeting, cancellation, cleanup, and tenant isolation.
3. Run differential output checks against the frozen Phase 00 legacy baseline. Explain changes due to complete page processing separately from LangGraph/VLM decisions.
4. With reviewer-frozen labels and owner thresholds, score legacy, graph-only, and VLM-challenger variants on the same holdout. Produce per-document failures, association audit, and a recommendation about VLM promotion. If labels or thresholds are absent, report the gate open; do not invent a pass.
5. Run a representative bounded concurrent test on the local host for code behavior. Capture queue depth/age, throughput, p95, errors, RSS/VRAM/scratch. Full target-host soak and GPU image checks remain in `GPU_LOAD_TEST.MD`.
6. Update docs/runbook, migration and rollback notes. Produce a final matrix of passed/failed/open gates and retain evidence hashes privately.

Deliverables: real integration tests, comparison and capacity reports, updated docs, issue list, and code-track sign-off recommendation. Exit when the test lane is technically reviewable and every unmet external acceptance item is named plainly.
