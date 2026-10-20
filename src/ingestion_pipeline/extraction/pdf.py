import logging
from html import escape
from pathlib import Path

import pymupdf as fitz
import pymupdf4llm

log = logging.getLogger("ingest.pdf")


def _is_pdf_password_error(exc: BaseException) -> bool:
    """True if `exc`, or anything it wraps via exception chaining, is
    pdfminer's PDFPasswordIncorrect.

    pdfplumber re-wraps internal pdfminer exceptions into its own
    `pdfplumber.utils.exceptions.PdfminerException` (confirmed: it
    arrives with an empty message and is NOT a subclass of
    PDFPasswordIncorrect) - so a plain `isinstance(e, PDFPasswordIncorrect)`
    check on what pdfplumber.open() raises never matches, even for a
    genuinely password-protected file. Exception chaining (`raise ...
    from e`, or a bare `raise` inside an except block) is what preserves
    the original underneath - walk __cause__/__context__ to find it
    instead of trusting the outer wrapper type.
    """
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, PDFPasswordIncorrect):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False

# Import OCR rendering defaults (DPI and max dimension) from the central config.
# Mid-file import is intentional: this module is imported by both CPU-only and
# GPU entry points, and config must be resolved after the module's own constants.
from ..config import LARGE_PDF_PAGE_COUNT, LARGE_PDF_SIZE_BYTES, OCR_DPI, OCR_MAX_DIM  # noqa: E402


def is_page_scanned(page) -> bool:
    """Per-page check: does this single page have a usable digital text layer?"""
    text = page.extract_text() or ""
    return len(text.strip()) <= 30


def is_pdf_encrypted(pdf_path: str) -> bool:
    """True if the PDF requires a password to open at all.

    Checked with PyMuPDF (already a dependency here) BEFORE anything
    tries to open the file with pdfplumber/pdfminer - opening a
    password-protected PDF with no password raises an uncaught
    pdfminer.pdfdocument.PDFPasswordIncorrect deep inside pdfplumber,
    which previously reached the ASGI layer as a raw 500 instead of a
    clean is_password_required flag (mirroring how excel.py's
    _is_encrypted checks .xlsx/.xls before opyxl/xlrd ever touch them).

    Uses `doc.needs_pass` rather than `doc.is_encrypted`: a PDF can be
    "encrypted" purely to restrict permissions (printing/copying) with
    only an owner password set, and PyMuPDF opens and reads that kind
    just fine with no password - `needs_pass` is specifically True only
    when a password is actually required to open/view the content,
    which is what is_password_required is meant to communicate.
    """
    try:
        doc = fitz.open(pdf_path)
        needs_pass = doc.needs_pass
        doc.close()
        return needs_pass
    except Exception as e:
        # If we can't even tell, don't guess it's a password issue here -
        # log it so it's visible, and let normal processing attempt it
        # and fail/report as a genuine unreadable file instead. Note:
        # some real-world "protected" PDFs (certificate-based security,
        # DRM policies, non-standard Encrypt dicts - common from banks/
        # credit bureaus) don't set the standard flag PyMuPDF checks
        # here at all, so this pre-check can miss them; manager.py has
        # a second, more specific check (pdfminer's PDFPasswordIncorrect)
        # at the point pdfplumber actually tries to open the file, which
        # catches what this pre-check doesn't.
        log.debug("Could not determine encryption status for %s: %s", pdf_path, e)
        return False

def md_table_to_html(md_text: str) -> str:
    """Convert a Markdown table to standard HTML table for schema consistency."""
    lines = [line.strip() for line in md_text.strip().splitlines() if line.strip()]
    if not lines or not lines[0].startswith("|"):
        return md_text
    html = ["<table>"]
    in_header = True
    for i, line in enumerate(lines):
        if "---" in line:
            in_header = False
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        tag = "th" if in_header and i == 0 else "td"
        row_str = "<tr>" + "".join(f"<{tag}>{escape(c)}</{tag}>" for c in cells) + "</tr>"
        html.append(row_str)
    html.append("</table>")
    return "".join(html)

def parse_digital_pdf_to_blocks(pdf_path: str, page_nums: set[int] | None = None) -> list[dict]:
    """Extract structured blocks using pymupdf4llm for layout-aware Markdown and tables.

    Uses pymupdf4llm with lightweight ONNX Runtime CPU layout detection:
    - Automatically preserves document hierarchy (headings, sections, metadata)
    - Automatically structures tables without layout interleaving
    - Zero GPU VRAM usage and sub-second execution on CPU
    """
    blocks = []
    order = 0
    source_file = Path(pdf_path).name

    try:
        selected = sorted(page_num - 1 for page_num in page_nums) if page_nums is not None else None
        chunks = pymupdf4llm.to_markdown(pdf_path, page_chunks=True, pages=selected, use_ocr=False)
    except Exception as e:
        log.warning("pymupdf4llm extraction failed for %s, falling back to PyMuPDF: %s", pdf_path, e)
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, start=1):
                if page_nums is not None and page_num not in page_nums:
                    continue
                text = page.get_text().strip()
                if text:
                    blocks.append({
                        "block_label": "text",
                        "content": text,
                        "order": order,
                        "page": page_num,
                        "confidence": 1.0,
                        "source_file": source_file,
                        "format": "pdf",
                    })
                    order += 1
        return blocks

    for chunk in chunks:
        meta = chunk.get("metadata", {})
        page_num = meta.get("page_number", 1)
        if page_nums is not None and page_num not in page_nums:
            continue

        raw_text = chunk.get("text", "")
        sections = [s.strip() for s in raw_text.split("\n\n") if s.strip()]

        for section in sections:
            if section.startswith("|") and "|" in section:
                label = "table"
                content = md_table_to_html(section)
            elif section.startswith("#"):
                label = "heading"
                content = section
            else:
                label = "text"
                content = section

            blocks.append({
                "block_label": label,
                "content": content,
                "order": order,
                "page": page_num,
                "confidence": 1.0,
                "source_file": source_file,
                "format": "pdf",
            })
            order += 1

    return blocks

def is_large_or_dense_pdf(pdf_path: str) -> bool:
    """Check file size and page count to identify heavy uncompressed scans."""
    size = Path(pdf_path).stat().st_size
    with fitz.open(pdf_path) as doc:
        pages = doc.page_count
    return size > LARGE_PDF_SIZE_BYTES or pages > LARGE_PDF_PAGE_COUNT

def render_pdf_page_to_image(pdf_path: str, page_num: int, out_dir: str,
                             dpi: int = OCR_DPI, max_dim: int = OCR_MAX_DIM) -> str:
    """Render a 1-indexed PDF page to a PNG for OCR processing.

    The image is rendered at the requested ``dpi`` (default OCR_DPI, e.g. 200-250)
    and uniformly down-scaled if either dimension exceeds ``max_dim`` to stay
    within model input limits.
    """
    effective_dpi = dpi if (dpi and dpi > 0) else OCR_DPI
    effective_dim = min(max_dim if (max_dim and max_dim > 0) else OCR_MAX_DIM, 4000)
    with fitz.open(pdf_path) as doc:
        if page_num < 1 or page_num > doc.page_count:
            raise ValueError("PDF page number is out of range")
        page = doc[page_num - 1]
        width, height = page.rect.width, page.rect.height
        if width <= 0 or height <= 0:
            raise ValueError("PDF page has invalid dimensions")
        zoom = effective_dpi / 72.0
        zoom = min(zoom, effective_dim / max(width, height), (16_000_000 / (width * height)) ** 0.5)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        out_path = Path(out_dir) / f"{Path(pdf_path).stem}_p{page_num}.png"
        pix.save(str(out_path))
        return str(out_path)
