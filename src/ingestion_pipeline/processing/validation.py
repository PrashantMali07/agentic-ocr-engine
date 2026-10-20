import difflib
import json
import logging
import re
from pathlib import Path

from ..core.utils import normalize_text, text_similarity, word_coverage

log = logging.getLogger("ingest.processing.validation")

LOW_CONFIDENCE_THRESHOLD = 0.80
LOW_COVERAGE_THRESHOLD = 0.90
LOW_SIMILARITY_THRESHOLD = 0.85

def get_docx_raw_text(path: str) -> str:
    """Extract raw text in true document order."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(path)
    parts = []

    def iter_block_items(document):
        parent_elm = document.element.body
        for child in parent_elm.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield Table(child, document)

    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            if item.text.strip():
                parts.append(item.text)
        elif isinstance(item, Table):
            for row in item.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        parts.append(cell.text)

    return "\n".join(parts)

def get_html_raw_text(path: str) -> str:
    from bs4 import BeautifulSoup
    html_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html_text, "lxml")
    return soup.get_text(separator="\n", strip=True)

def get_pdf_native_text_per_page(path: str) -> list[str] | None:
    """Extract each page's embedded text layer."""
    try:
        import pymupdf
        doc = pymupdf.open(path)
        pages = [page.get_text() for page in doc]
        doc.close()
        return pages
    except Exception as e:
        log.warning("Could not open PDF for native text extraction: %s", e)
        return None

def blocks_text(blocks: list[dict], page: int | None = None) -> str:
    """Concatenate block content for a source file, optionally filtered to one page."""
    selected = [b for b in blocks if b["block_label"] in ("text", "heading", "table")]
    if page is not None:
        selected = [b for b in selected if b.get("page") == page]

    parts = []
    for b in selected:
        content = str(b.get("content", ""))
        if b["block_label"] == "table":
            try:
                from bs4 import BeautifulSoup
                content = BeautifulSoup(content, "lxml").get_text(separator=" ", strip=True)
            except Exception:
                content = re.sub(r"<[^>]+>", " ", content)
        else:
            parts.append(content)
    return "\n".join(parts)

def validate_docx_or_html(original_path: str, fmt: str, blocks: list[dict]) -> dict:
    raw_text = get_docx_raw_text(original_path) if fmt == "docx" else get_html_raw_text(original_path)
    extracted = blocks_text(blocks)

    coverage = word_coverage(extracted, raw_text)
    similarity = text_similarity(extracted, raw_text)

    flags = []
    if coverage < LOW_COVERAGE_THRESHOLD:
        flags.append(f"Word coverage {coverage:.0%} below {LOW_COVERAGE_THRESHOLD:.0%} threshold")
    if similarity < LOW_SIMILARITY_THRESHOLD:
        flags.append(f"Text similarity {similarity:.0%} below {LOW_SIMILARITY_THRESHOLD:.0%} threshold")

    return {
        "method": "native_text_diff",
        "word_coverage": round(coverage, 4),
        "text_similarity": round(similarity, 4),
        "flags": flags,
    }

def validate_pdf(original_path: str, blocks: list[dict], num_pages: int) -> dict:
    native_pages = get_pdf_native_text_per_page(original_path)
    page_reports = []
    flags = []

    if native_pages is None:
        return {"method": "unavailable", "flags": ["Could not open PDF"], "pages": []}

    for page_num in range(1, num_pages + 1):
        native_text = native_pages[page_num - 1] if page_num - 1 < len(native_pages) else ""
        extracted = blocks_text(blocks, page=page_num)

        if not native_text.strip():
            page_blocks = [b for b in blocks if b.get("page") == page_num]
            scores = [b.get("confidence") for b in page_blocks if isinstance(b.get("confidence"), (int, float))]
            if not scores:
                page_reports.append({"page": page_num, "method": "ocr_confidence", "avg_confidence": None})
                flags.append(f"Page {page_num}: scanned page, no OCR data")
            else:
                avg_conf = sum(scores) / len(scores)
                page_reports.append({"page": page_num, "method": "ocr_confidence", "avg_confidence": round(avg_conf, 4)})
                if avg_conf < LOW_CONFIDENCE_THRESHOLD:
                    flags.append(f"Page {page_num}: avg OCR confidence {avg_conf:.0%} below threshold")
        else:
            similarity = text_similarity(extracted, native_text)
            coverage = word_coverage(extracted, native_text)
            page_reports.append({"page": page_num, "method": "native_text_diff", "text_similarity": round(similarity, 4), "word_coverage": round(coverage, 4)})
            if similarity < LOW_SIMILARITY_THRESHOLD:
                flags.append(f"Page {page_num}: text similarity {similarity:.0%} below threshold")

    return {"method": "mixed", "pages": page_reports, "flags": flags}

def validate_image(blocks: list[dict]) -> dict:
    scores = [b.get("confidence") for b in blocks if isinstance(b.get("confidence"), (int, float))]
    unscored = [b for b in blocks if not isinstance(b.get("confidence"), (int, float))]
    flags = []
    if not scores:
        flags.append("No OCR confidence data available")
        return {"method": "ocr_confidence", "avg_confidence": None, "flags": flags}

    avg_conf = sum(scores) / len(scores)
    if avg_conf < LOW_CONFIDENCE_THRESHOLD:
        flags.append(f"Avg OCR confidence {avg_conf:.0%} below threshold")
    if unscored:
        flags.append(f"{len(unscored)} block(s) had no confidence score")

    return {"method": "ocr_confidence", "avg_confidence": round(avg_conf, 4), "flags": flags}

def validate_email(email_record: dict, attachments_dir: str) -> dict:
    attachments_dir = Path(attachments_dir)
    all_blocks = email_record["blocks"]
    reports = {}
    all_flags = []

    for att in email_record["attachments"]:
        fname = att["file"]
        fmt = att["format"]
        original_path = attachments_dir / fname
        file_blocks = [b for b in all_blocks if b["source_file"] == fname]

        if not original_path.exists():
            reports[fname] = {"method": "unavailable", "flags": ["Original file not found"]}
            all_flags.append(f"{fname}: original missing")
            continue

        try:
            if fmt == "docx":
                report = validate_docx_or_html(str(original_path), "docx", file_blocks)
            elif fmt in ("html", "htm"):
                report = validate_docx_or_html(str(original_path), "html", file_blocks)
            elif fmt == "pdf":
                report = validate_pdf(str(original_path), file_blocks, att.get("pages") or 1)
            else:
                report = validate_image(file_blocks)
        except Exception as e:
            log.warning("Validation failed for %s: %s", fname, e)
            report = {"method": "error", "flags": [f"Error: {e}"]}

        reports[fname] = report
        all_flags.extend([f"{fname}: {f}" for f in report.get("flags", [])])

    return {
        "email_id": email_record["email_id"],
        "per_attachment": reports,
        "flags": all_flags,
        "needs_review": len(all_flags) > 0,
    }

def get_diff(
    email_record: dict,
    attachments_dir: str,
    filename: str,
    page: int | None = None,
) -> dict | None:
    """
    Generate a unified diff between original and extracted text.
    Returns a dict with diff details, or None if diff not available.
    """
    attachments_dir = Path(attachments_dir)
    original_path = attachments_dir / filename
    att = next((a for a in email_record["attachments"] if a["file"] == filename), None)
    if att is None:
        return None

    file_blocks = [b for b in email_record["blocks"] if b["source_file"] == filename]
    fmt = att["format"]

    if fmt == "docx":
        original_text = get_docx_raw_text(str(original_path))
        extracted_text = blocks_text(file_blocks)
        page_label = ""
    elif fmt in ("html", "htm"):
        original_text = get_html_raw_text(str(original_path))
        extracted_text = blocks_text(file_blocks)
        page_label = ""
    elif fmt == "pdf":
        native_pages = get_pdf_native_text_per_page(str(original_path))
        if native_pages is None:
            return None
        if page is None:
            # For PDFs, return diff for first page with content
            for i, native_text in enumerate(native_pages):
                if native_text.strip():
                    page = i + 1
                    break
            else:
                return None
        original_text = native_pages[page - 1] if page - 1 < len(native_pages) else ""
        extracted_text = blocks_text(file_blocks, page=page)
        page_label = f" (page {page})"
    else:
        # No ground truth for images
        return None

    similarity = text_similarity(extracted_text, original_text)
    coverage = word_coverage(extracted_text, original_text)

    diff_lines = list(difflib.unified_diff(
        normalize_text(original_text).split(". "),
        normalize_text(extracted_text).split(". "),
        fromfile="original", tofile="extracted", lineterm="",
    ))

    return {
        "filename": filename + page_label,
        "similarity": round(similarity, 4),
        "coverage": round(coverage, 4),
        "diff": "\n".join(diff_lines) if diff_lines else "No differences",
    }


def show_diff(email_record: dict, attachments_dir: str, filename: str, page: int | None = None):
    """Print diff to console (CLI usage)."""
    result = get_diff(email_record, attachments_dir, filename, page)
    if result is None:
        print(f"No diff available for '{filename}'.")
        return
    print(f"=== {result['filename']} ===\n")
    print(f"Similarity: {result['similarity']:.1%}")
    print(f"Coverage:   {result['coverage']:.1%}\n")
    print(result["diff"])

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate extracted content.")
    parser.add_argument("--json", required=True, help="Path to <email_id>.json.")
    parser.add_argument("--attachments", required=True, help="Path to attachments directory.")
    parser.add_argument("--output", default=None, help="Output report path.")
    parser.add_argument("--diff", nargs=2, metavar=("FILENAME", "PAGE"), help="Print diff.")
    args = parser.parse_args()

    with open(args.json) as f:
        record = json.load(f)

    if args.diff:
        filename, page = args.diff
        show_diff(record, args.attachments, filename, page=int(page) if int(page) > 0 else None)
        return

    result = validate_email(record, args.attachments)
    out_path = Path(args.output) if args.output else Path(args.json).parent / f"{result['email_id']}_validation.json"
    out_path.write_text(json.dumps(result, indent=2))
    log.info("Saved: %s", out_path)

if __name__ == "__main__":
    main()
