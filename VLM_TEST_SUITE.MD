# VLM-assisted OCR test-suite preparation

Status: **review-preparation code is available** (19 October 2026). No document was sent to a VLM, and no ground-truth label has been accepted. The first target is the five scanned PDFs `test_asset/RFQ_06_scanned.pdf` through `RFQ_10_scanned.pdf`. Their companion JPGs are alternate representations of the same documents, **not** five additional independent samples.

## Purpose and boundary

A vision-language model (VLM) can draft a full transcription, reading order, table rows/columns, and key fields from rendered page images. Compare our PP-StructureV3/Tesseract pipeline against a **human-corrected reference**, not directly against the raw VLM answer. Document VLMs can omit, misread, or invent visual text; document-OCR benchmarks therefore use human-verified answers and explicitly test text-grounding errors ([OCRBench v2](https://arxiv.org/abs/2501.00321), [CC-OCR](https://arxiv.org/abs/2412.02210)). Agreement between the VLM and our OCR is not proof that either one read the image correctly.

The VLM only prepares candidate labels. An independent reviewer sees the source image and makes the final decision; the business owner sets acceptance thresholds before scores are calculated. Keep the VLM draft separate from the existing `ground_truth_template.json` so draft text cannot accidentally masquerade as verified reference data.

The code path is `scripts/prepare_vlm_review.py`. It renders each page to a private, hash-linked manifest, calls a locally selected VLM runner for structured drafts, creates an editable image-review template, and refuses to finalize ground truth until every page and field is marked verified. This repository does not select or download VLM weights. The runner receives `--image /absolute/page.png`, reads the page-specific prompt on stdin, and writes one JSON object to stdout. The script makes no network call itself; the operator must review the runner's data handling.

```bash
PYTHONPATH=src .venv/bin/python scripts/prepare_vlm_review.py render --output-dir phase04_review
PYTHONPATH=src .venv/bin/python scripts/prepare_vlm_review.py draft --manifest phase04_review/manifest.json --runner /absolute/path/to/local-vlm-runner --model-id approved-local-model-version
PYTHONPATH=src .venv/bin/python scripts/prepare_vlm_review.py review-template --drafts phase04_review/vlm_drafts/drafts.json --output phase04_review/review_template.json
# An independent reviewer edits a copy at phase04_review/reviewed.json using the PDF/images.
PYTHONPATH=src .venv/bin/python scripts/prepare_vlm_review.py finalize --review phase04_review/reviewed.json --output phase04_review/ground_truth.json
```

The `phase04_review` directory is mode 0700 and outputs are mode 0600; existing files are never overwritten. The review template includes full page transcripts, uncertain spans, field values, page/label/row/column locations, and source excerpts. The reviewer must set `verified_by`, `verified_at`, and each `verified_from_image` flag after checking the image. These flags document a review; software cannot prove that the visual inspection occurred.

## Step 1 — Freeze and render the corpus

1. Inventory each source PDF's SHA-256, page count, dimensions, scan/digital status, language, table density, and difficulty. Use PDFs as the canonical sources; record whether a companion JPG is byte/visually equivalent. Keep all versions of one document in the same train/development/holdout partition to prevent leakage.
2. Render each PDF page to an image at a fixed, documented DPI and color mode. Keep the original PDF and an unmodified render; if crops/deskew/contrast variants help the VLM, record their transforms and source page coordinates. Hash each rendered image. Never silently drop a blank-looking page.
3. Store the private corpus manifest and rendered images under ignored, owner-only `phase04_review/` or `phase06_evidence/`. Do not commit document images, draft transcripts, or OCR output. If a cloud VLM is considered, the data owner must approve that provider's handling and retention terms first; a local VLM avoids uploading the source documents. Use a separate machine or test window so VLM inference does not contaminate OCR GPU-load measurements.

## Step 2 — Generate structured VLM drafts

Choose and record one VLM version, weights/provider, inference parameters, prompt version, image preprocessing, and run date. Send each page image **without our OCR text** to avoid anchoring the candidate answer to the system being evaluated. Use deterministic settings where supported and ask for structured JSON with:

- Full visible text in reading order, preserving numbers, units, punctuation, and page breaks.
- Tables as explicit row/column/cell records, with header names and page number; retain empty cells.
- Every requested RFQ field with exact visible value and its label or table row/column evidence.
- An `uncertain`/unreadable marker for ambiguous text rather than a guessed completion.

An illustrative draft record is:

```json
{
  "source": "RFQ_06_scanned.pdf",
  "page": 1,
  "transcript": "…",
  "fields": [
    {"key": "value_1", "value": "125", "label": "Price", "row": 1, "column": "Value", "evidence": "…"},
    {"key": "value_2", "value": "180", "label": "Price", "row": 2, "column": "Value", "evidence": "…"}
  ],
  "uncertain": []
}
```

Validate the JSON schema, source/page identifiers, nonempty text, and required-field coverage, but do not treat a well-formed draft as correct. Keep the raw response and provenance in `phase04_review/vlm_drafts/`. A second independent VLM pass can flag disagreements for reviewer attention; it cannot replace the reviewer.

## Step 3 — Independent image-based review

1. The reviewer opens each canonical PDF page beside the VLM draft and checks the **entire** transcript, including headers, footers, small print, table cells, repeated values, and reading order. They correct omitted or invented text by looking at the image, not at our OCR output. For ambiguous characters, retain an explicit adjudication note and have a second reviewer resolve material uncertainty.
2. For each field, verify both the value and its location: the exact label, row, column, and page. Example: if source row A has price 125 and row B has 180, a draft or OCR result that swaps them has both values present but fails both price associations. Keep an itemized association checklist privately; `correct` and `total` in the release JSON must be traceable to that checklist.
3. Sign off the corrected reference with reviewer identity/date and exact source-PDF hashes. Do not fill `verified_by` with a VLM name. Freeze the file before running or tuning the OCR pipeline. Reserve additional representative scans as a holdout set; five RFQ scans are the current minimum gate, not a claim of broad production accuracy.

The existing `scripts/prepare_phase04_review.py` still creates a blank, private five-scan template without using a VLM. It now includes `field_locations` for the association review. Populate its transcript, fields, and locations from the source images. Preserve VLM drafts separately. Both paths produce ground truth accepted by `scripts/evaluate_phase04_acceptance.py` after review.

## Step 4 — Compare with our OCR and analyze errors

1. Run the current OCR pipeline on the **same canonical PDFs**, separately from VLM drafting. After independent review, create a private (0700) `phase06_evidence/` directory and run `PYTHONPATH=src .venv/bin/python scripts/evaluate_phase04_acceptance.py --ground-truth phase04_review/ground_truth.json --device gpu --private-extracted-output phase06_evidence/extracted.json > phase06_evidence/accuracy.json`. The private extracted file contains document text and is ignored by Git. Do not run this before the reference is frozen.
2. Compute CER, WER, and field-value presence using the evaluator. Run `scripts/prepare_phase04_associations.py template --ground-truth phase04_review/ground_truth.json --extracted phase06_evidence/extracted.json --output phase06_evidence/association_template.json`. An independent reviewer fills every observed value, page/label/row/column, OCR excerpt, verdict, and reason for incorrect items, then sets `reviewed_from_image_and_ocr`, identity, and date. Save the edited copy and run `scripts/prepare_phase04_associations.py finalize --ground-truth phase04_review/ground_truth.json --extracted phase06_evidence/extracted.json --review phase06_evidence/association_reviewed.json --output phase06_evidence/associations.json`. The script recomputes totals; the release gate validates all items and source/OCR hashes. Association remains a manual image-and-output review because the API does not emit normalized business-field objects.
3. Review per-document and per-page differences, not only aggregate percentages. Classify omissions, substitutions, hallucinated text, wrong reading order, lost pages, table-cell shifts, and wrong-field associations. Compare the raw VLM draft with the verified reference as a **label-preparation error analysis**; compare OCR with the verified reference as the actual acceptance result.
4. Run `scripts/check_phase06_quality.py` only after the business owner signs the numeric policy and the GPU load report exists. Its digest binding prevents mixing accuracy/association reports from different reference-file bytes. If OCR fails, fix the pipeline and rerun against the frozen holdout; do not edit the reference to make a score pass.

## Acceptance of this preparation method

The method is ready when every source page has a hash-linked reviewed transcript, every scored field has a reviewed value and association, material reviewer disagreements are adjudicated, the reference is frozen, and another person can reproduce the same OCR score from the same source files. VLM output by itself is useful diagnostic evidence, **not** an independent acceptance label.
