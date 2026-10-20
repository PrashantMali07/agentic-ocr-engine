# Phase 04 execution plan — VLM-assisted reference corpus

1. Confirm source hashes and page counts for the five RFQ scans and choose additional representative scanned, digital, mixed, table-heavy, and low-quality documents from permitted local assets. Keep source variants together in one split.
2. Exercise existing `prepare_vlm_review.py` render/draft/review-template/finalize commands with a selected local runner. Record model identity, weights checksum, prompt version, decoding settings, render DPI, and run date. Do not expose document images to an unapproved hosted provider.
3. Add focused tests for manifest tampering, runner timeout, malformed or oversized JSON, duplicate fields, lost/swapped pages, and incomplete review. Add a reproducible private evidence layout and instructions for the independent reviewer.
4. Have an independent reviewer inspect every page image, correct the full transcript and field/table associations, mark uncertainty, and sign the reference. This is a human task; engineering cannot mark it complete on the reviewer's behalf.
5. Obtain owner-approved numeric thresholds before scoring. Freeze reference hashes and holdout. Run legacy-only scoring and preserve itemized error categories for Phase 05/06 comparison.

Deliverables: runnable local VLM draft workflow, validated private evidence, reviewed reference if a reviewer is available, frozen split/threshold record if an owner is available, and test results. Code exit is separate from human accuracy sign-off.
