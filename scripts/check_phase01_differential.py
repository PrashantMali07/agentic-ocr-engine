"""Phase 01 differential baseline: legacy caller vs shared service on real fixtures.

Runs every canonical fixture once through each caller with the *same* parser
stack (PP-StructureV3 + Tesseract on the GPU for scanned pages; CPU pymupdf4llm
for digital pages), then compares page accounting, block equivalence, and
per-page empty/failed outcomes. Evidence lands in ``phase01_evidence/``
(gitignored): hashes + metrics only, no document text is written.

Run:
    uv run python scripts/check_phase01_differential.py [--split splits]...
splits: digital, scanned, images, all (default all)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ingestion_pipeline.extraction import manager, service  # noqa: E402

TEST_ASSET = REPO_ROOT / "test_asset"
EVIDENCE_DIR = REPO_ROOT / "phase01_evidence"

CANONICAL = [
    ("digital", ["RFQ_01_digital.pdf", "RFQ_02_digital.pdf", "RFQ_03_digital.pdf",
                 "RFQ_04_digital.pdf", "RFQ_05_digital.pdf"]),
    ("scanned", ["RFQ_06_scanned.pdf", "RFQ_07_scanned.pdf", "RFQ_08_scanned.pdf",
                 "RFQ_09_scanned.pdf", "RFQ_10_scanned.pdf"]),
    ("images", ["RFQ_06_scanned.jpg", "RFQ_07_scanned.jpg", "RFQ_08_scanned.jpg",
                "RFQ_09_scanned.jpg", "RFQ_10_scanned.jpg"]),
]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tokens(text: str) -> set[str]:
    import re

    return {token.lower() for token in re.findall(r"[A-Za-z0-9]{4,}", text)}


def legacy_call(path: Path, work_dir: Path) -> dict:
    record = manager.process_email_attachments([path], str(work_dir / "legacy_out"), "differential")
    att = next(a for a in record["attachments"] if a["file"] == path.name)
    blocks = [b for b in record["blocks"] if b.get("source_file") == path.name]
    return {
        "page_count": att.get("pages"),
        "processed_pages": att.get("processed_pages"),
        "empty_pages": att.get("empty_pages"),
        "is_password_required": bool(att.get("is_password_required")),
        "blocks": blocks,
    }


def service_call(path: Path, work_dir: Path) -> dict:

    plan = service.plan_document_pages(service.LEGACY_DOCUMENT_ID, path)
    outcomes = []
    if plan.entries[0].file_format == "pdf":
        digital_blocks = service.extract_digital_pages(plan.entries, path)
        service_outcomes: list[service.PageOutcome] = []
        for entry in plan.entries:
            if entry.classification == "digital":
                page_blocks = [b for b in digital_blocks if b.get("page") == entry.page_number]
                status = "completed" if page_blocks else "empty"
                service_outcomes.append(service.PageOutcome(entry=entry, blocks=page_blocks, status=status))
            else:
                work = service.PageWork(entry=entry, file_path=path)
                service_outcomes.append(service.run_pdf_page(work))
        outcomes = service_outcomes
    elif plan.entries[0].file_format in {"docx", "html", "htm", "xlsx", "xls", "csv"}:
        outcomes = service.run_native_document(plan, path)
    else:
        outcomes = [service.run_image_page(service.PageWork(entry=plan.entries[0], file_path=path))]
    blocks = [b for outcome in outcomes for b in outcome.blocks]
    page_count = plan.total_pages
    empty = [o.entry.page_number for o in outcomes if o.status == "empty"]
    failed = [o.entry.page_number for o in outcomes if o.status == "failed"]
    return {
        "page_count": page_count,
        "processed_pages": [o.entry.page_number for o in outcomes],
        "empty_pages": empty,
        "failed_pages": failed,
        "is_password_required": False,
        "blocks": blocks,
    }


def _text_of(blocks: list[dict]) -> str:
    return "\n\n".join(str(b.get("content", "")) for b in blocks)


def compare(name: str, legacy: dict, shared: dict) -> dict:
    page_match = legacy["page_count"] == shared["page_count"]
    processed_match = legacy["processed_pages"] == shared["processed_pages"]
    empty_match = sorted(legacy.get("empty_pages") or []) == sorted(shared.get("empty_pages") or [])
    legacy_text = _text_of(legacy["blocks"])
    shared_text = _text_of(shared["blocks"])
    exact_text = legacy_text == shared_text
    lt, st = _tokens(legacy_text), _tokens(shared_text)
    overlap = len(lt & st) / max(1, len(lt | st)) if (lt or st) else 1.0
    failures = []
    if not page_match:
        failures.append(f"page_count {legacy['page_count']} != {shared['page_count']}")
    if not processed_match:
        failures.append(f"processed_pages {legacy['processed_pages']} != {shared['processed_pages']}")
    if not empty_match:
        failures.append(f"empty_pages {legacy.get('empty_pages')} != {shared.get('empty_pages')}")
    if not exact_text and overlap < 0.95:
        failures.append(f"block text overlap {overlap:.3f}")
    return {
        "fixture": name,
        "legacy_sha256": None,  # filled by caller
        "page_count_match": page_match,
        "processed_pages_match": processed_match,
        "empty_pages_match": empty_match,
        "block_text_exact": exact_text,
        "token_overlap": round(overlap, 6),
        "legacy_blocks": len(legacy["blocks"]),
        "shared_blocks": len(shared["blocks"]),
        "legacy_empty_pages": legacy.get("empty_pages"),
        "shared_empty_pages": shared.get("empty_pages"),
        "shared_failed_pages": shared.get("failed_pages"),
        "pass": not failures,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--splits", nargs="+", default=["all"],
                        choices=("digital", "scanned", "images", "all"))
    args = parser.parse_args()
    split_names = (["digital", "scanned", "images"] if args.splits == ["all"] else args.splits)

    results = []
    for split, names in CANONICAL:
        if split not in split_names:
            continue
        for name in names:
            source = TEST_ASSET / name
            if not source.is_file():
                results.append({"fixture": name, "missing": True, "pass": False})
                continue
            source_sha = sha256_of(source)
            print(f"=== {name} (sha256={source_sha[:16]}...) ===", flush=True)
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    work_dir = Path(tmp)
                    legacy = legacy_call(source, work_dir)
                    shared = service_call(source, work_dir)
                    record = compare(name, legacy, shared)
                    record["legacy_sha256"] = source_sha
                    record.pop("missing", None)
            except Exception as exc:  # a fixture failing either caller is a real finding
                record = {
                    "fixture": name, "legacy_sha256": source_sha, "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(record)
            status = "PASS" if record.get("pass") else "FAIL"
            print(f"    -> {status}: pages={record.get('page_count_match')}, "
                  f"overlap={record.get('token_overlap')}", flush=True)

    passed = sum(1 for r in results if r.get("pass"))
    summary = {
        "schema": "phase01-differential-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "callers": ["extraction.manager.process_email_attachments (legacy worker path)", "extraction.service (shared service path)"],
        "engine": "PP-StructureV3 + PaddleOCR (GPU) + Tesseract reconciliation / CPU digital path",
        "fixtures": results,
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
    }
    EVIDENCE_DIR.mkdir(exist_ok=True)
    out = EVIDENCE_DIR / "differential_baseline.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out} ({passed}/{len(results)} passed)")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
