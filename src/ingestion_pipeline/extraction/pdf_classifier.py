"""Simple heuristics to decide whether a PDF should be processed with
low-resource settings (low DPI, aggressive cleanup) or high-quality settings.
"""

from pathlib import Path

import pymupdf as fitz  # pymupdf

from ..config import (
    LARGE_PDF_PAGE_COUNT,
    LARGE_PDF_SIZE_BYTES,
)


def _get_pdf_stats(pdf_path: str) -> tuple[int, int]:
    """Return (file_size_bytes, page_count)."""
    size = Path(pdf_path).stat().st_size
    doc = fitz.open(pdf_path)
    pages = doc.page_count
    doc.close()
    return size, pages

def should_use_low_dpi(pdf_path: str) -> bool:
    """
    True → use low-DPI / low-max-dim settings (suited for large PDFs).
    False → use high-DPI / high-max-dim settings (suited for small files, screenshots).
    """
    size, pages = _get_pdf_stats(pdf_path)
    return size > LARGE_PDF_SIZE_BYTES or pages > LARGE_PDF_PAGE_COUNT
