# Phase 01 limitations — resolved and remaining (updated 20 October 2026)

Phase 01 (shared bounded extraction) delivered:
- `extraction/service.py` — single page-truth and per-page work boundary
- `extraction/manager.py` — legacy worker delegates its PDF/image branches to it
- `storage/artifacts.py` — bounded artifact-ID store (plus Phase 02 deterministic writes)

Current suite status: **140 passed, exit 0** (`uv run python -m pytest tests -q`).

## CLOSED since the original write-up

### ✅ 1. Differential baseline over the full fixture set — EXECUTED AND PASSING
`scripts/check_phase01_differential.py` ran every canonical fixture through both
callers with the same parser stack (PP-StructureV3 + PaddleOCR server models on
GPU + Tesseract reconciliation; CPU pymupdf4llm digital path):

- **15/15 fixtures passed** — 5 digital PDFs, 5 scanned PDFs (GPU), 5 scanned
  images (GPU)
- Block text is **byte-exact** between callers (`block_text_exact=true` on every
  fixture; token overlap 1.0 — the stronger 0.95 gate was never needed)
- Page accounting identical everywhere (`page_count_match`, `processed_pages_match`,
  `empty_pages_match` all true); zero password/failed-page findings

Evidence (hash-bound, gitignored): `phase01_evidence/differential_baseline.json`

Re-run anytime:
```bash
uv run python scripts/check_phase01_differential.py --splits all
```
Exit 0 confirms parity; any split can be run alone (`--splits digital|--splits scanned|--splits images`).

### ✅ 2. Scanned visualization artifacts
Documented as a deliberate boundary: direct service consumers persist page artifacts
as deterministic artifacts (`ocr` kind) instead of free-form visualization paths;
the legacy caller's `save_visualizations` behavior is unchanged.

### ✅ 3. The `page_idx=0` repair point (Phase 02 scope)
`temporal/activities.py` was fully rewritten onto the shared service in Phase 02 —
the mismatch is gone, and the lane processes **every planned page**
(verified live for a mixed 3-page PDF through a real workflow environment and
through the standalone harness `scripts/test_temporal_workflow.py`).

## STILL REMAINING (not blockers for Phase 03)

1. ~~**Real Temporal server durability suite**~~ — ✅ CLOSED 20 Oct 2026:
   `tests/integration/test_temporal_lane_live.py` ran against a live
   `temporal server start-dev` instance — duplicate admission joined the same
   workflow and a SIGKILLed mid-flight worker recovered to an exactly-once
   result (2/2 passed in 40s). The suite skips itself when no server is up.
2. **Fixture split freeze** — proposal drafted in
   `phase00_evidence/fixture_freeze.json` (holdout = RFQ_09 + RFQ_10 scanned
   pairs, everything else development). Awaiting owner sign-off; the
   acceptance rule aborts scoring on any (name, sha256) mismatch.
3. **Phase 04+ sign-offs** — VLM drafts are generated and review-binding
   (`phase04_review/oct20_run/`): drafts via `gemma4:31b-cloud` + review
   template ready for the independent human reviewer; owner-set thresholds
   recorded before scoring are still owed (external gates).
4. **Lane for general traffic stays disabled** until Phase 06 acceptance gates.
5. **Static-analysis gates** added: `ruff` + `mypy` configured in `pyproject.toml`;
   both pass clean (`ruff` across `src/tests/scripts`, `mypy` strict on
   `contracts/`, `storage/`, `temporal/`). Widen `mypy` coverage later —
   `extraction/`, `graph/`, `api/` remain unwatched so far.

## Environment facts verified on this machine (20 Oct 2026)
- `uv run ingest-api` boots; `/health` returns `{"status": "healthy"}`.
- GPU OCR loads cached Paddle models (`PP-OCRv5_server_*`, PP-StructureV3) with no
  downloads needed; a full 15-fixture differential completes in minutes.
- Ollama serves `gemma4:31b-cloud` (remote-routed) and the VLM runner verified
  end-to-end: `echo '<prompt>' | scripts/vlm_runner --image <png>` → exit 0.
- Per decision, OpenAI/Gemini providers stay implemented-but-uncalled. The
  selected provider and model determine where inference runs; the owner will
  select a local model before testing with real documents.
