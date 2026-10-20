# Phase 00 execution plan — contracts and baseline

1. Inventory current response/status fields and the active SQLite worker's claim rules. Note compatibility constraints for existing clients.
2. Select contract module placement; define typed schemas and version tags, including explicit unknown/partial states and one-based pages.
3. Inventory `test_asset` without copying it into source control. Create a private fixture manifest with hashes, page counts, format categories, and chosen train/development/holdout grouping.
4. Add schema validation and round-trip tests. Record representative legacy outputs for later differential checks; do not call those outputs ground truth.
5. Review the contracts against Temporal payload size, LangGraph state, API authorization, and review targeting. Freeze V1 before Phase 01 implementation.

Deliverables: typed V1 contracts, contract examples, fixture manifest, schema tests, compatibility map, and a short open-decision log (VLM choice and eventual distributed topology may remain open).

Exit when every later work package can name its input/output types and when a reviewer can identify which component owns each state transition. No service behavior changes are required in this phase.
