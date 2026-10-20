# Phase 03 execution plan — LangGraph route and audit decisions

1. Map each existing node in `graph/nodes.py` to either extraction work (move behind shared service/Temporal activity) or a decision (retain in LangGraph). Preserve useful auditor signals and table-association checks.
2. Define a small graph state and typed decision output. Remove numpy arrays and raw document payloads from state. Add a strict policy version and pass counter.
3. Implement route, audit, and reconciliation graphs or subgraphs. Start with deterministic policy rules; a model-assisted policy would be a separate versioned experiment rather than an uncontrolled prompt deciding arbitrary tools.
4. Have Temporal call the graph decision activity, validate the returned action, dispatch the matching activity, and record reason codes in progress metadata. Route `needs_review` into the Phase 02 durable wait.
5. Add branch tests, bounded-loop tests, malformed-result tests, and Temporal dispatch integration. Run existing graph tests and update them where behavior intentionally changes.

Deliverables: bounded graph state, route/audit policies, Temporal dispatch integration, branch matrix, and a trace sample. Exit when LangGraph makes observable decisions without becoming a second scheduler or storing image-filled checkpoints.
