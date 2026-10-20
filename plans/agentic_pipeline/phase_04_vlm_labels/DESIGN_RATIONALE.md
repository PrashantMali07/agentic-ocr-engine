# Phase 04 design rationale

A VLM can make labeling faster but can also omit small text, swap table rows, or invent a plausible value. Comparing OCR directly against raw VLM output would only measure agreement between two imperfect systems. An independent image-based review supplies the reference; the VLM draft is a productivity aid and an error source to analyze.

Exact value presence is weaker than association. If a table has prices 125 and 180 and OCR swaps the rows, a value-presence metric can pass while the business meaning is wrong. The reference therefore records page, label, row/column, and evidence for each field; the acceptance report must score these associations separately.

The existing script already enforces important integrity checks, so the remaining engineering work is runner reproducibility, adversarial tests, corpus breadth, and reviewer usability. Five closely related RFQ scans can validate the workflow but cannot establish general production accuracy. Keeping holdout documents frozen prevents tuning the route/VLM policy to the answer key.

Owner-set thresholds are a policy decision, not an engineering default. Present per-document distributions and failure examples so a business owner can choose a meaningful limit before scores are revealed as pass/fail.
