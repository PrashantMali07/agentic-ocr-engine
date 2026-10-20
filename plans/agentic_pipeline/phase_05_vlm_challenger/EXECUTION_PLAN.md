# Phase 05 execution plan — runtime VLM challenger

1. Choose a permitted local model and runner; record weights/version, license/privacy boundary, prompt, preprocessing, and hardware needs. Check that the runner can process the rendered-page format from Phase 01.
2. Implement a mockable adapter and strict candidate schema. Use the same artifact store and page identifiers as OCR, but separate namespaces and retention for raw VLM candidates.
3. Add a dedicated Temporal activity and bounded VLM worker/queue. Implement timeout, cancellation, hard process limit, output cap, idempotent commit, and explicit unavailable/error states.
4. Add LangGraph trigger and reconciliation decisions with a maximum of one VLM challenge per page. Preserve OCR candidate and all uncertainty; route material disagreement to human review.
5. Test adversarial/invalid runner output and concurrency behavior. Run OCR-only and OCR+VLM variants against the frozen holdout without changing labels or thresholds. Capture per-document accuracy and capacity tradeoffs.

Deliverables: test-only VLM adapter, worker/queue, graph/Temporal integration, negative tests, and a side-by-side report. Exit when the branch is reproducible and fails safely; passing a production-promotion gate is a separate decision.
