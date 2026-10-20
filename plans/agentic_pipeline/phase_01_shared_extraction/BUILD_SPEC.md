# Phase 01 build spec — shared bounded extraction

Estimate: **5–7 engineer-days** after Phase 00. Preserve the legacy API contract while extracting reusable format/page work.

## Target modules and behavior

- Add a shared dispatcher such as `src/ingestion_pipeline/extraction/service.py` with typed `preflight_document`, `iter_page_work`, `extract_native`, `extract_scanned_page`, and `aggregate_document` boundaries. Reuse `processing/preflight.py` and existing parsers rather than cloning their logic.
- Route PDF pages individually: digital text may use native extraction; scanned or mixed pages use bounded rendering and PP-StructureV3/Tesseract. Process **all** pages and retain one-based page provenance, source order, explicit per-page errors, and partial status.
- Route DOCX/HTML/CSV/XLS/XLSX through their native parsers where supported. Image inputs become one or more pages according to the existing support policy. Reject unsupported combinations clearly.
- Keep PaddleOCR, layout/table reconstruction, and Tesseract reconciliation in one shared processing path. Preserve original recognized values and correction provenance; avoid silent replacements of monetary amounts or identifiers.
- Add a tenant-scoped artifact interface (`source`, `render`, `ocr`, `result`) with generated IDs, size/hash metadata, atomic write, bounded read, and cleanup. Single-host implementation may use the existing output root; do not embed raw local paths in contracts.

Likely edit points: `extraction/manager.py`, `pdf.py`, `pdf_classifier.py`, `ocr.py`, `hybrid.py`, `native.py`, `processing/preflight.py`, `services/job_runner.py`, `storage/`, and response schemas. Keep the existing worker as a caller of the shared service; do not replace its scheduler here.

## Invariants and acceptance

Input and decoded-resource limits apply before expensive rendering/inference. Only bounded page batches reside in memory. Every output page has exactly one terminal state and a source page number; missing pages prevent complete success. The same fixture processed through the legacy worker and direct shared service has equivalent visible output or an explicitly reviewed difference. Tests cover digital/scanned/mixed multipage PDF, tables, Office/CSV, corrupted/password-protected inputs, and high-expansion/oversize rejection.
