# Phase 03 design rationale

LangGraph is useful here for explicit branching and auditable quality decisions. Temporal is useful for durable scheduling. Letting both independently retry OCR, maintain review state, and persist large graph state would cause double work and conflicting truth. The graph therefore returns a decision; Temporal executes it and owns time.

The current graph is deterministic despite the “agent” name. That is acceptable for a first agentic architecture: it can choose among extraction, fallback, VLM challenge, and human review based on evidence. A later LLM planner is not needed to meet the testing goal and would add nondeterminism and prompt-injection exposure from document content. If introduced, it must return one validated action from a finite set and be compared against the deterministic policy.

Model confidence alone is not a reliable business-field verification signal. Route decisions should consider missing pages, table shape, repeated values, source-to-result association, parser errors, and disagreement between candidates. An uncertain value is preserved with provenance and sent to review instead of silently corrected.

Stateless or bounded graph invocations make Temporal replay and restart behavior easier to reason about. A development checkpointer may help debug a sample, but it must not become the production review database.
