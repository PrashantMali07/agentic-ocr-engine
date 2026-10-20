"""Shared bounded extraction service (Phase 01).

One implementation of format dispatch and complete-page PDF work, reused by
both the legacy worker (``extraction.manager``) and the Temporal test lane
(``temporal.activities``), so a fixture produces the same page counts and
per-page outcomes through either caller — no first-page-only behavior.

Design invariants:

- ``plan_pages`` is the single source of page truth for a PDF: it classifies
  every page once (digital vs scanned by the same ``is_page_scanned`` test
  used today) and never drops or merges pages.
- Scanned pages are processed strictly one at a time; only one rendered page
  image and one OCR page result are alive at once (bounded memory).
- The service returns small typed records (``PageWork``, ``PageOutcome``)
  carrying metadata and provenance. Document text inside block payloads
  stays in the existing block dicts exactly as the legacy path produced.
- OCR/reconciliation helpers are invoked lazily so the legacy module's
  patch targets (used heavily in tests) stay authoritative; the service
  resolves them through a supplier-style indirection instead of importing
  them by value.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal, Sequence

from ..processing.preflight import PreflightError, check_document
from .pdf import is_page_scanned

log = logging.getLogger("ingest.extraction.service")

DOC_EXTS = {".docx"}
HTML_EXTS = {".html", ".htm"}
EXCEL_EXTS = {".xlsx", ".xls"}
CSV_EXTS = {".csv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
NATIVE_EXTS = DOC_EXTS | HTML_EXTS | EXCEL_EXTS | CSV_EXTS
ALL_SERVICE_EXTS = {".pdf"} | IMAGE_EXTS | NATIVE_EXTS

# Placeholder manifest identity for legacy-work caller bookkeeping: the
# legacy path keys per-file metadata by filename, so per-document IDs are
# meaningful only in the Temporal test lane.
LEGACY_DOCUMENT_ID = "0" * 32


def extract_digital_pages(entries: Sequence[PagePlanEntry], path: Path) -> list[dict]:
    """Native extraction for every planned digital page of one document."""
    from .pdf import parse_digital_pdf_to_blocks

    page_nums = {e.page_number for e in entries if e.classification == "digital"}
    if not page_nums:
        return []
    return parse_digital_pdf_to_blocks(str(path), page_nums=page_nums)


@dataclass(frozen=True)
class PagePlanEntry:
    """One page of one document, classified once at manifest time."""

    document_id: str
    file_name: str
    file_format: str          # lowe rcase extension without the dot
    page_number: int          # one-based across the whole document
    classification: Literal["digital", "scanned"]
    total_pages: int

    @property
    def needs_scan_render(self) -> bool:
        return self.classification == "scanned"


PageClassification = Literal["digital", "scanned"]


@dataclass
class PageWork:
    """Per-page inputs resolved immediately before work (bounded lifetime)."""

    entry: PagePlanEntry
    file_path: Path
    render_path: Path | None = None   # rendered page image for a scanned page
    pipeline: object | None = None    # shared OCR pipeline, resolved per page


@dataclass
class PageOutcome:
    """One terminal outcome per page. ``blocks`` carries the page's blocks
    exactly as the legacy parser shaped them."""

    entry: PagePlanEntry
    blocks: list[dict] = field(default_factory=list)
    error: tuple[str, str] | None = None       # (code, message)
    status: Literal["completed", "empty", "failed"] = "completed"

    @property
    def explicit_error(self) -> bool:
        return self.error is not None


@dataclass(frozen=True)
class DocumentPlan:
    """Full page manifest for one admitted job's documents, in source order."""

    entries: list[PagePlanEntry]

    def pages(self) -> Iterator[PagePlanEntry]:
        yield from self.entries

    @property
    def total_pages(self) -> int:
        return len(self.entries)


def classify_document(path: Path) -> tuple[int, Callable[[], Sequence[PageClassification]]]:
    """Open the document once and return (page_count, per-page classifier).

    The returned classifier closes over the pages' text-existence evidence
    determined at plan time; later callers evaluate pages one at a time
    without re-opening the PDF.
    """
    import pdfplumber  # bounded import; heavy stack stays local to callers

    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = list(pdf.pages)
            total = len(pages)
            if total < 1:
                raise PreflightError("page_limit", "PDF has no pages")
            scanned = {index + 1 for index, page in enumerate(pages) if is_page_scanned(page)}
    except PreflightError:
        raise
    except Exception as exc:
        if _is_pdf_password_error(exc):
            raise PreflightError("password_required", "Document requires a password") from exc
        raise PreflightError("corrupt_document", "PDF text layer cannot be read") from exc

    def classifier() -> Sequence[PageClassification]:
        return [
            "scanned" if (page in scanned) else "digital"
            for page in range(1, total + 1)
        ]

    return total, classifier


def _is_pdf_password_error(exc: BaseException) -> bool:
    from .pdf import _is_pdf_password_error as _impl

    return _impl(exc)


def plan_document_pages(document_id: str, path: Path) -> DocumentPlan:
    """Build the full PageManifest for one document.

    Unified preflight + per-page classification: unsupported formats and
    bounded limits are enforced here once, and every page receives a
    classified, one-based plan entry. Nothing is processed yet.
    """
    path = Path(path)
    check_document(path)                      # bounded limits, password checks
    extension = path.suffix.lower()

    if extension == ".pdf":
        total, classifier = classify_document(path)
        return DocumentPlan(entries=[
            PagePlanEntry(
                document_id=document_id,
                file_name=path.name,
                file_format="pdf",
                page_number=page_number,
                classification=classification,
                total_pages=total,
            )
            for page_number, classification in enumerate(classifier(), start=1)
        ])

    if extension in IMAGE_EXTS:
        return DocumentPlan(entries=[PagePlanEntry(
            document_id=document_id,
            file_name=path.name,
            file_format=extension.lstrip("."),
            page_number=1,
            classification="scanned",
            total_pages=1,
        )])

    if extension in NATIVE_EXTS:
        return DocumentPlan(entries=[PagePlanEntry(
            document_id=document_id,
            file_name=path.name,
            file_format=extension.lstrip("."),
            page_number=1,
            classification="digital",
            total_pages=1,
        )])

    raise PreflightError("unsupported_format", "Document format is unsupported")


def plan_job_pages(documents: Sequence[tuple[str, Path]]) -> DocumentPlan:
    """Plan every page of every document in admission order."""
    entries: list[PagePlanEntry] = []
    for document_id, path in documents:
        entries.extend(plan_document_pages(document_id, path).entries)
    return DocumentPlan(entries=entries)


# ---------------------------------------------------------------------------
# Per-page work iterators.
# ---------------------------------------------------------------------------

def iter_pdf_page_work(plan: DocumentPlan, pdf_path: Path) -> Iterator[PageWork]:
    """Yield PageWork items for a PDF's planned pages, one at a time.

    A page's render/image lifetime is deliberately released by the caller
    before the next page is yielded, keeping at most one page image in
    memory across the whole document.
    """
    for entry in plan.entries:
        if entry.file_format == "pdf":
            yield PageWork(entry=entry, file_path=pdf_path)


def _ocr_helpers():
    """Resolve shared OCR/reconciliation helpers lazily.

    Imported through ``manager`` so tests can keep patching
    ``manager.get_ocr_pipeline``/``prepare_image_for_ocr``/``ocr_predict``/
    ``extract_tesseract_lines``/``pp_structure_to_blocks`` and the service
    picks up those patches (they are the same module attributes).
    """
    from . import manager as manager_module

    return {
        "get_ocr_pipeline": manager_module.get_ocr_pipeline,
        "prepare_image_for_ocr": manager_module.prepare_image_for_ocr,
        "ocr_predict": manager_module.ocr_predict,
        "extract_tesseract_lines": manager_module.extract_tesseract_lines,
        "pp_structure_to_blocks": manager_module.pp_structure_to_blocks,
        "reconcile_paddle_and_tesseract": manager_module.reconcile_paddle_and_tesseract,
        "render_pdf_page_to_image": manager_module.render_pdf_page_to_image,
    }


def run_pdf_page(
    work: PageWork,
    *,
    save_visualizations: bool = False,
    visualization_dir: Path | None = None,
) -> PageOutcome:
    """Extract one classified PDF page through the shared OCR pipeline.

    Digital pages go through the native PDF parser restricted to that page
    number; scanned pages render, OCR, reconcile, and release the image
    before returning. Exactly one terminal outcome is returned.
    """
    entry = work.entry
    if entry.classification == "digital":
        from .pdf import parse_digital_pdf_to_blocks

        blocks = parse_digital_pdf_to_blocks(str(work.file_path), page_nums={entry.page_number})
        status = "completed" if blocks else "empty"
        return PageOutcome(entry=entry, blocks=blocks, status=status)

    helpers = _ocr_helpers()

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            img_path = helpers["render_pdf_page_to_image"](str(work.file_path), entry.page_number, tmp_dir)
            pipeline = helpers["get_ocr_pipeline"]() if work.pipeline is None else work.pipeline
            img_arr = helpers["prepare_image_for_ocr"](img_path)
            page_results = helpers["ocr_predict"](pipeline, img_arr)
            if len(page_results) != 1:
                return PageOutcome(
                    entry=entry, status="failed",
                    error=("ocr_incomplete", "OCR did not return exactly one result for a PDF page"),
                )
            result = page_results[0]
            raw_blocks = helpers["pp_structure_to_blocks"](result, entry.file_name, entry.page_number)
            tess_lines = helpers["extract_tesseract_lines"](img_arr)
            reconciled = helpers["reconcile_paddle_and_tesseract"](
                raw_blocks, tess_lines,
                source_file=entry.file_name, page_num=entry.page_number, file_format="pdf",
            )
            if save_visualizations and visualization_dir is not None:
                vis_dir = visualization_dir / Path(entry.file_name).stem
                vis_dir.mkdir(parents=True, exist_ok=True)
                result.save_to_img(str(vis_dir))
            status = "completed" if reconciled else "empty"
            return PageOutcome(entry=entry, blocks=reconciled, status=status)
        except PreflightError:
            raise
        except Exception as exc:
            return PageOutcome(
                entry=entry, status="failed",
                error=("extraction_failure", f"{type(exc).__name__} while processing page {entry.page_number}"),
            )
        finally:
            # Only one page image exists at a time; release it eagerly.
            work.render_path = None


def run_image_page(work: PageWork, *, save_visualizations: bool = False,
                   visualization_dir: Path | None = None) -> PageOutcome:
    """Extract one standalone image document through the shared pipeline."""
    entry = work.entry
    helpers = _ocr_helpers()
    try:
        pipeline = helpers["get_ocr_pipeline"]() if work.pipeline is None else work.pipeline
        img_arr = helpers["prepare_image_for_ocr"](str(work.file_path))
        results = helpers["ocr_predict"](pipeline, img_arr)
        if len(results) != 1:
            return PageOutcome(
                entry=entry, status="failed",
                error=("ocr_incomplete", "OCR did not return exactly one result for an image"),
            )
        tess = helpers["extract_tesseract_lines"](img_arr)
        raw_blocks = helpers["pp_structure_to_blocks"](results[0], entry.file_name, entry.page_number)
        reconciled = helpers["reconcile_paddle_and_tesseract"](
            raw_blocks, tess,
            source_file=entry.file_name, page_num=entry.page_number,
            file_format=entry.file_format,
        )
        if save_visualizations and visualization_dir is not None:
            vis_dir = visualization_dir / Path(entry.file_name).stem
            vis_dir.mkdir(parents=True, exist_ok=True)
            results[0].save_to_img(str(vis_dir))
        status = "completed" if reconciled else "empty"
        return PageOutcome(entry=entry, blocks=reconciled, status=status)
    except PreflightError:
        raise
    except Exception as exc:
        return PageOutcome(
            entry=entry, status="failed",
            error=("extraction_failure", f"{type(exc).__name__} while processing image"),
        )


def run_native_document(plan: DocumentPlan, path: Path, *,
                        image_out_dir: Path | None = None) -> list[PageOutcome]:
    """Extract a native (docx/html/excel/csv) document as a single-page plan."""
    entry = plan.entries[0]
    extension = entry.file_format
    from .csv_parser import parse_csv
    from .excel import parse_excel
    from .native import parse_document

    out_dir = str(image_out_dir) if image_out_dir is not None else "./extracted_images"
    try:
        if extension == "docx" or extension in {"html", "htm"}:
            record = parse_document(str(path), image_out_dir=out_dir)
        elif extension == "csv":
            record = parse_csv(str(path), image_out_dir=out_dir)
        elif extension in {"xlsx", "xls"}:
            record = parse_excel(str(path), image_out_dir=out_dir)
            if record.get("_password_required"):
                raise PreflightError("password_required", "Document requires a password")
        else:
            raise PreflightError("unsupported_format", "Document format is unsupported")
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError("corrupt_document", "Document cannot be parsed") from exc

    blocks = record.get("blocks") or []
    if not blocks:
        label = {"csv": "CSV", "xlsx": "Spreadsheet", "xls": "Spreadsheet"}.get(extension, "Document")
        raise PreflightError("empty_document", f"{label} contains no extractable content")
    normalized = []
    source_name = Path(str(path)).name
    for block in blocks:
        block.setdefault("source_file", source_name)
        block.setdefault("format", record.get("format", extension))
        normalized.append(block)
    outcome = PageOutcome(entry=entry, blocks=normalized, status="completed")
    return [outcome]


def aggregate_outcomes(outcomes: Iterable[PageOutcome], file_name: str) -> dict:
    """Reduce per-page outcomes into per-file metadata.

    Preserves one-based source order and explicit per-page failure; a page
    with blocks and a page without never collapse into the same state.
    Missing pages surface to the caller as gaps in ``processed_pages``.
    """
    pages = list(outcomes)
    processed = [o.entry.page_number for o in pages]
    failed = [o.entry.page_number for o in pages if o.status == "failed"]
    empty = [o.entry.page_number for o in pages if o.status != "failed" and not o.blocks]
    blocks = [b for o in pages for b in o.blocks if b.get("source_file") == file_name]
    confidences = [
        b["confidence"] for b in blocks
        if isinstance(b.get("confidence"), (int, float)) and 0.0 <= b["confidence"] <= 1.0
    ]
    return {
        "file": file_name,
        "pages": max(processed) if processed else None,
        "processed_pages": processed,
        "empty_pages": sorted(set(empty) - set(failed)),
        "failed_pages": sorted(failed),
        "blocks": blocks,
        "avg_confidence": (
            round(sum(confidences) / len(confidences), 4)
            if confidences else None
        ),
    }
