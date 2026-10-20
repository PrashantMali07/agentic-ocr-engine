# Phase 00 design rationale

The existing routes, graph, and Temporal workflow use different shapes and lifecycle assumptions. A shared contract prevents three near-identical pipelines from drifting. Versioning is needed because Temporal jobs may outlive a code release; silently changing the shape of an in-flight activity result can break replay or reconciliation.

Stable page IDs and hashes make missing-page detection possible. They also bind VLM drafts, OCR output, reviewer corrections, and accuracy scores to the same source bytes. The current five RFQ scans are useful for early regression but too narrow for a broad accuracy claim.

Artifact references keep sensitive and bulky data out of workflow history and graph checkpoints. A reference must resolve through tenant-scoped authorization and integrity checks; an arbitrary filesystem path is not an acceptable replacement. The plan keeps compatibility data at the API edge while internal contracts become stricter.

Decision to record: whether the first test lane supports all currently accepted formats or only PDF/image. Recommendation: the shared service covers all supported formats, while VLM challenge is limited to rendered pages with documented policy. No caller should silently route Office/CSV files to an image decoder.
