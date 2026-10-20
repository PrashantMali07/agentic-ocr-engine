# Phase 06 design rationale

Unit tests can validate routing and schema behavior but cannot prove that a job survives a crash between SQLite admission and Temporal start, that a worker restart reuses an artifact safely, or that GPU contention stays bounded. Those claims require integrated tests with the actual server/worker boundaries and failure injection.

Comparing three modes on the same frozen reference isolates the value of each architectural addition. The Temporal+LangGraph path should not be credited for parser changes introduced by the shared service without a separate differential note. The VLM path should be judged by verified field association and review behavior as well as text error rates; a lower CER that swaps two prices is not an improvement.

Resource limits are observable properties of a topology. A local concurrency test can validate code backpressure, but the intended GPU host, container image, driver, ingress, and backup/alert setup are separate release evidence. The plan therefore distinguishes “code track complete” from “production accepted.”

Keeping the legacy route available until acceptance provides a comparison and rollback path. Already-running Temporal jobs still need a drain/finish policy if a later traffic switch is rolled back; an API flag alone does not cancel or migrate durable workflows.
