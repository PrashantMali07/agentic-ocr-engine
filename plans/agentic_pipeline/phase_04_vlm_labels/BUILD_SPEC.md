# Phase 04 build spec — VLM-assisted reference corpus

Estimate: **3–5 engineer-days**, starting after Phase 00 and overlapping Phases 01–03. This is test-data preparation, not runtime extraction and not Phase 4 business sign-off by itself.

## Reuse and extend existing tooling

`scripts/prepare_vlm_review.py` already renders the five `test_asset/RFQ_06_scanned.pdf`–`RFQ_10_scanned.pdf` documents, hashes page images, invokes a local executable VLM runner, validates draft JSON, prepares a reviewer template, and finalizes image-verified labels. `scripts/prepare_phase04_review.py`, `evaluate_phase04_acceptance.py`, `prepare_phase04_associations.py`, and `check_phase06_quality.py` cover related review and scoring steps. This phase verifies and extends those tools instead of rewriting them.

Required additions: a documented local runner contract and pinned model/prompt provenance; tests for malformed, truncated, oversized, page-swapped, and hallucinated drafts; a private review manifest that binds source PDF, render, draft, corrected transcript, field association, reviewer identity/date, and evaluation split. Keep the local runner isolated from OCR outputs. Expand the corpus beyond the five RFQ scans for later broad claims, with the document and companion images in the same partition. Do not commit source images, draft transcripts, or reviewed text.

## Review and scoring contract

Every page transcript must be checked against the source image. Every scored field must retain exact value plus page, label, row/column, and evidence excerpt; repeated equal values require unambiguous association. Ambiguous text remains marked and adjudicated. The business owner signs numeric limits (CER, WER, field value/association, and permitted review rate) before any acceptance score is evaluated. Freeze hashes and holdout split before tuning OCR or VLM policy.

## Acceptance

A second person can reproduce page renders and confirm hashes, inspect all VLM drafts and human edits, and run the evaluator on a frozen reviewer-approved reference. The tooling rejects an unreviewed draft or a source/manifest mismatch. Human reviewer time and owner approval are external to the engineering-day estimate; absent sign-off, code may be complete but accuracy acceptance remains open.
