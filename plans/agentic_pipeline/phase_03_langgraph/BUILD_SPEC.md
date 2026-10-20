# Phase 03 build spec — LangGraph route and audit decisions

Estimate: **4–6 engineer-days** after the Phase 02 activity contract exists. LangGraph is an internal decision component within the Temporal test lane.

## Graph contract and nodes

- Replace image arrays, local paths, raw OCR token lists, tables, and final document text in `graph/state.py` with page metadata, artifact IDs, bounded quality signals, attempt counters, and typed `RouteDecisionV1`/audit outcomes.
- Split the current all-in-one graph into bounded decisions: preflight route (`native` or `legacy_ocr`), post-extraction audit (`accept`, `fallback`, `vlm_challenge`, `review`, `fail`), and post-challenge reconciliation. Each decision has explicit inputs, reason codes, evidence references, and a policy version.
- Temporal dispatches the selected native/OCR/fallback/VLM activity. LangGraph returns an action; it does not hold a GPU slot while waiting for another queue and does not create its own durable production job.
- Keep a maximum number of passes and reject repeated cycles. A missing result, missing confidence, page mismatch, or unresolved association leads to `review`/`fail`, not `completed`.
- Remove `MemorySaver` from the normal Temporal decision path. A fresh bounded graph invocation can be stateless; a development-only checkpointer may remain isolated. Graph `interrupt()` is not used for production review.

Likely edit points: `graph/state.py`, `graph/nodes.py`, `graph/workflow.py`, `extraction/auditor.py`, `temporal/activities.py`, and typed contracts. Existing graph CLI or gated synchronous behavior must either call the new shared service or be clearly deprecated without exposing a second production lifecycle.

## Acceptance

Fixture tests prove deterministic decisions for digital PDF, clean scan, weak OCR, table/field ambiguity, failed fallback, and review. Decision trace contains reason codes and artifact IDs, not document text. Replaying the same input/policy yields the same action unless a versioned model result differs. No graph branch silently accepts an empty final result or loops indefinitely. The Temporal workflow can dispatch every allowed action and treats unknown actions as failure.
