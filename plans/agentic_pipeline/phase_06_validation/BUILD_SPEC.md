# Phase 06 build spec — integration, comparison, and code-track acceptance

Estimate: **5–8 engineer-days** after Phases 01–05. This closes the planned code track, not production operations or image release gates.

## Required integrated scenarios

- Real authenticated FastAPI multipart admission to each allowed test-lane mode, polling, tenant isolation, review submission/history, expiry, and cancellation. The supported legacy route remains the control.
- Real Temporal test server with CPU/GPU/VLM workers: API/reconciler crash windows, server/worker restart, duplicate starts, activity retry, hard timeout, partial page failure, review pause/resume, and replay-compatible workflow changes.
- Document matrix: digital, scanned, mixed and multipage PDFs; tables with repeated values; image and native formats; encrypted/corrupt/oversize inputs. Assert exact page accounting and explicit partial/failed outcomes.
- Capacity matrix: multiple simultaneous files and large bounded files; global/per-tenant queue limits; one OCR GPU process; VLM contention policy; peak RSS/VRAM/scratch, queue age, p95 latency, and no unbounded result/history growth.
- Quality comparison on frozen reviewed labels: legacy only, Temporal+LangGraph with legacy OCR, and that path with VLM challenge. Report CER/WER, value presence, value-to-label/row/column association, false review, missed review, and per-document error categories. Apply owner-set thresholds only if signed before scoring.

Use `tests/integration/` for real service tests and `tests/security/` for auth/storage boundary regression. Extend `scripts/evaluate_phase04_acceptance.py`, `prepare_phase04_associations.py`, and existing load scripts only where needed; keep private evidence in ignored directories. Update `README.md`, `TEMPORAL_GUIDE.md`, API schema, and local runbook to match actual behavior, including disabled features.

## Final code gate

All existing security/integration tests and new lane tests pass. No cross-tenant result/artifact access, silent page loss, duplicate logical result, stranded admitted job, unlimited graph loop, or uncontrolled GPU concurrency remains in the tested topology. Every unresolved defect has severity, owner, and reproduction. The final report distinguishes code-complete, reviewer/owner acceptance, GPU image/load, and production operations statuses. Do not switch production traffic or claim release readiness from code tests alone.
