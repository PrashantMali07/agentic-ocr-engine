# Phase 01 execution plan — shared bounded extraction

1. Build a differential baseline from Phase 00 fixtures: text, table cells, page counts, warnings, and status. Record known legacy defects separately.
2. Define a single render contract in `extraction/pdf.py` (page numbering, DPI, pixel ceiling, transform metadata, and close/cleanup behavior). Repair the graph/Temporal page-0 and renderer call mismatches through the shared API.
3. Implement bounded page iteration and format dispatch. Persist large intermediates as checked artifacts; pass small page metadata between stages. Close native parser handles and remove scratch data in `finally` paths.
4. Move or wrap existing legacy extraction behind the shared service without changing `/extract/v1` admission, auth, or job claiming. Avoid duplicating OCR and reconciliation algorithms.
5. Add focused tests for every advertised format, all-page accounting, mixed digital/scanned pages, hostile/oversize inputs, and cleanup after failure. Run existing security and integration tests.
6. Compare fixture output to the frozen baseline; document intentional differences and verify field/table associations on affected samples.

Deliverables: shared service, artifact abstraction, page-aware results, regression tests, and a compatibility note. Exit when no supported multipage scan is silently reduced to page 1 and the active legacy path still passes its tests.
