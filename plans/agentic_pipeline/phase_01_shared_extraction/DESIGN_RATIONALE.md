# Phase 01 design rationale

The current experimental graph and Temporal activities each render the first scanned PDF page, while the supported legacy pipeline has its own extraction logic. Fixing each route independently would create separate OCR semantics and repeated security limits. One shared service makes later comparisons meaningful: a difference then comes from orchestration or VLM decisions, not an accidental parser fork.

Temporal is a coordinator, not a file-stream processor. Large PDFs need bounded preflight, page iteration, storage, and process limits regardless of the scheduler. Releasing page resources after each bounded batch protects a 12 GB-class GPU and limits scratch growth when many files arrive.

Separate native extraction from image OCR to avoid spending GPU slots on digital text. Mixed PDFs need per-page classification; a document-level `is_digital` flag cannot represent one scanned attachment page inside an otherwise digital PDF.

The artifact interface is intentionally small in the local test lane. It can later map to object storage without making workflow logic depend on absolute paths. Artifact integrity and tenant scope are required from the first version so the later distributed adapter does not weaken the boundary.
