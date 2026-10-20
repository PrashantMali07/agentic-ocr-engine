"""Build the private Phase 00 fixture manifest from ``test_asset``.

Output is evidence, never source: the manifest lands in ``phase00_evidence/``
(gitignored) and carries hashes, sizes, page counts, and format categories
only. No document text is extracted into the manifest, and nothing under
``test_asset/`` is copied into source control (``/test_asset`` is ignored).

Classification policy (bounded and deterministic):
- ``digital``: multi-page PDF whose maximum per-page embedded text length is
  at least MIN_TEXT_CHARS on every page inspected.
- ``scanned``: PDF whose maximum per-page embedded text length is below that
  threshold (scan/OCR-bound input).
- ``image``: raster files.
- ``archive``: zip bundles kept out of the default fixture selection.
Run:  uv run python -m scripts.build_phase00_fixture_manifest [--all]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import fitz  # pymupdf
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ASSET = REPO_ROOT / "test_asset"
EVIDENCE_DIR = REPO_ROOT / "phase00_evidence"
MANIFEST_PATH = EVIDENCE_DIR / "fixture_manifest.json"

MIN_TEXT_CHARS = 40
MAX_FILE_BYTES = 40 * 1024 * 1024  # refuse pathological inputs during inventory
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
ARCHIVE_EXTENSIONS = {".zip"}

# Canonical fixture selection: the RFQ corpus the plans name as the initial
# scanned subset plus its digital counterparts.
CANONICAL_PATTERN = "RFQ_*"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_pdf(path: Path) -> dict:
    doc = fitz.open(path)
    try:
        page_count = doc.page_count
        max_chars = 0
        password_protected = bool(doc.needs_pass)
        if not password_protected:
            for page in doc:
                max_chars = max(max_chars, len(page.get_text(clip=None)))
        classification = "digital" if max_chars >= MIN_TEXT_CHARS else "scanned"
        return {
            "page_count": page_count,
            "classification": classification,
            "password_protected": password_protected,
            "max_page_text_chars": max_chars,
        }
    finally:
        doc.close()


def classify_image(path: Path) -> dict:
    with Image.open(path) as image:
        return {
            "page_count": 1,
            "classification": "image",
            "password_protected": False,
            "width": image.width,
            "height": image.height,
        }


def entry_for(path: Path) -> dict | None:
    stat = path.stat()
    if stat.st_size > MAX_FILE_BYTES:
        return None
    extension = path.suffix.lower()
    record = {
        "name": path.name,
        "sha256": sha256_of(path),
        "size_bytes": stat.st_size,
        "extension": extension,
        "role": "canonical" if path.match(CANONICAL_PATTERN) else "auxiliary",
    }
    try:
        if extension in PDF_EXTENSIONS:
            record.update(classify_pdf(path))
        elif extension in IMAGE_EXTENSIONS:
            record.update(classify_image(path))
        elif extension in ARCHIVE_EXTENSIONS:
            record.update({"page_count": None, "classification": "archive", "password_protected": False})
        else:
            return None
    except Exception as exc:  # corrupt/unreadable fixtures are inventory findings, not crashes
        record.update({"classification": "unreadable", "error": type(exc).__name__})
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Phase 00 fixture manifest")
    parser.add_argument("--all", action="store_true", help="include non-RFQ auxiliary artifacts")
    args = parser.parse_args()

    entries = []
    for path in sorted(TEST_ASSET.rglob("*")):
        if not path.is_file():
            continue
        record = entry_for(path)
        if record is None:
            continue
        if not args.all and record["role"] != "canonical":
            continue
        entries.append(record)

    manifest = {
        "schema": "phase00-fixture-manifest-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": "test_asset",
        "classification_policy": {
            "digital": f"PDF with >= {MIN_TEXT_CHARS} embedded text chars on max page",
            "scanned": "PDF below that threshold",
            "note": "embedded-text heuristic only; Phase 01 render contract supersedes it",
        },
        "fidelity_split": {
            "development": [e["name"] for e in entries if e["role"] == "canonical" and e.get("classification") == "digital"][:2],
            "holdout": [e["name"] for e in entries if e["role"] == "canonical" and e.get("classification") in {"digital", "scanned"}][-4:],
            "note": "proposal only; frozen (hash-listed) in Phase 01 before any tuning",
        },
        "fixtures": entries,
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {MANIFEST_PATH.relative_to(REPO_ROOT)} with {len(entries)} fixtures")
    counts: dict[str, int] = {}
    for record in entries:
        counts[record.get("classification", "?")] = counts.get(record.get("classification", "?"), 0) + 1
    print(f"Categories: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
