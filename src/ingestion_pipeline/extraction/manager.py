import logging
import math
from pathlib import Path
from typing import List

from ..processing.preflight import PreflightError, check_document
from . import service
from .csv_parser import parse_csv
from .excel import parse_excel

# Per-call patch seams: the shared service resolves these OCR/reconciliation
# helpers through manager attributes at call time, and the regression suite
# monkeypatches them here. The redundant alias form marks them as
# deliberate re-exports for the static-analysis gate.
from .hybrid import extract_tesseract_lines as extract_tesseract_lines  # noqa: F401
from .hybrid import reconcile_paddle_and_tesseract as reconcile_paddle_and_tesseract  # noqa: F401
from .native import parse_document
from .ocr import clear_gpu_cache, get_ocr_pipeline, ocr_predict
from .ocr import pp_structure_to_blocks as pp_structure_to_blocks  # noqa: F401
from .ocr import prepare_image_for_ocr as prepare_image_for_ocr  # noqa: F401
from .pdf import is_page_scanned as is_page_scanned  # noqa: F401
from .pdf import parse_digital_pdf_to_blocks as parse_digital_pdf_to_blocks  # noqa: F401
from .pdf import render_pdf_page_to_image as render_pdf_page_to_image  # noqa: F401

# File extension sets for supported attachment types
IMAGE_PDF_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
DOC_EXTS = {".docx"}
HTML_EXTS = {".html", ".htm"}
EXCEL_EXTS = {".xlsx", ".xls"}
CSV_EXTS = {".csv"}
ALL_SUPPORTED_EXTS = IMAGE_PDF_EXTS | DOC_EXTS | HTML_EXTS | EXCEL_EXTS | CSV_EXTS

log = logging.getLogger("ingest.extraction")

# Batching helper - limits the size of each batch to stay within GPU memory.
# Adjust MAX_BATCH_SIZE according to your GPU (8-16 works on most 12-GB cards).

MAX_BATCH_SIZE = 8

def _predict_batch(pipeline, images: List[object]) -> List[object]:
    """Send a list of pre-processed image ndarrays to the OCR pipeline.

    PaddleOCR's ``predict`` accepts a list (or any iterable) of NumPy arrays.
    For very long image lists we split into ``MAX_BATCH_SIZE`` chunks so we
    never exceed GPU memory.
    Returns a flat list of Result objects in the same order as ``images``.
    """
    results: List[object] = []
    for i in range(0, len(images), MAX_BATCH_SIZE):
        batch = images[i : i + MAX_BATCH_SIZE]
        # Thread-safe: get_ocr_pipeline() is a single shared instance, and
        # this may now be called concurrently across jobs (see ocr.py).
        batch_res = ocr_predict(pipeline, batch)
        results.extend(batch_res)
    return results


def collect_input_files(inputs: list[str]) -> list[Path]:
    """Expand a mix of files and directories into a flat list of supported
    attachment paths (pdf, image, docx, html/htm)."""

    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for ext in ALL_SUPPORTED_EXTS:
                files.extend(sorted(p.rglob(f"*{ext}")))
        elif p.is_file():
            if p.suffix.lower() in ALL_SUPPORTED_EXTS:
                files.append(p)
            else:
                log.warning("Skipping unsupported file type: %s", p)
        else:
            log.warning("Path not found, skipping: %s", p)

    seen = set()
    unique_files = []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique_files.append(f)
    return unique_files

def process_email_attachments(
    file_paths: list[Path],
    output_dir: str,
    email_id: str,
    save_visualizations: bool = False,
    request_id: str | None = None,
) -> dict:
    """Orchestrate extraction of all attachments for a single email.

    ``request_id`` is propagated to the logger so every log line for this run
    includes a unique identifier.
    """
    logger = logging.getLogger("ingest.extraction")
    adapter = logging.LoggerAdapter(logger, {"request_id": request_id or ""})

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    combined_blocks: list[dict] = []
    attachments_meta: list[dict] = []
    pp_pipeline = None

    for f in file_paths:
        check_document(f)
        ext = f.suffix.lower()
        previous_block_count = len(combined_blocks)

        if ext == ".pdf":
            # One page manifest per document from the shared service: every
            # page classified once, one-based, nothing dropped.
            doc_plan = service.plan_document_pages(service.LEGACY_DOCUMENT_ID, f)
            total_pages = doc_plan.total_pages
            digital_pages = [e.page_number for e in doc_plan.entries if e.classification == "digital"]
            scanned_pages = [e.page_number for e in doc_plan.entries if e.classification == "scanned"]

            if digital_pages:
                adapter.info("Processing %d digital page(s) natively: %s", len(digital_pages), f.name)
                combined_blocks.extend(service.extract_digital_pages(doc_plan.entries, f))

            if scanned_pages:
                adapter.info("Processing %d scanned page(s): %s", len(scanned_pages), f.name)
                pp_pipeline = get_ocr_pipeline()
                for page_num in sorted(scanned_pages):
                    entry = next(e for e in doc_plan.entries if e.page_number == page_num)
                    work = service.PageWork(entry=entry, file_path=f, pipeline=pp_pipeline)
                    outcome = service.run_pdf_page(
                        work,
                        save_visualizations=save_visualizations,
                        visualization_dir=output_dir / "visualized",
                    )
                    if outcome.error:
                        raise PreflightError(*outcome.error)
                    combined_blocks.extend(outcome.blocks)
                    del outcome  # release the page's OCR payload immediately

            attachments_meta.append({
                "file": f.name,
                "format": "pdf",
                "pages": total_pages,
                "digital_pages": sorted(digital_pages),
                "scanned_pages": sorted(scanned_pages),
                "processed_pages": list(range(1, total_pages + 1)),
                "empty_pages": sorted(set(range(1, total_pages + 1)) - {
                    b.get("page") for b in combined_blocks[previous_block_count:]
                    if b.get("source_file") == f.name and isinstance(b.get("page"), int)
                }),
                "is_password_required": False,
            })

        elif ext in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
            # Images run through the same shared service path (pipeline maxsize=1)
            pp_pipeline = get_ocr_pipeline()

            adapter.info("Processing Image (Hybrid PP-StructureV3 + Tesseract): %s", f.name)
            img_plan = service.plan_document_pages(service.LEGACY_DOCUMENT_ID, f)
            work = service.PageWork(entry=img_plan.entries[0], file_path=f, pipeline=pp_pipeline)
            outcome = service.run_image_page(
                work,
                save_visualizations=save_visualizations,
                visualization_dir=output_dir / "visualized",
            )
            if outcome.error:
                raise PreflightError(*outcome.error)
            combined_blocks.extend(outcome.blocks)
            del outcome

            attachments_meta.append({
                "file": f.name, "format": ext.lstrip("."), "pages": 1,
                "processed_pages": [1],
                "empty_pages": [] if len(combined_blocks) > previous_block_count else [1],
            })

        elif ext in {".docx", ".html", ".htm"}:
            adapter.info("Processing (native parser): %s", f.name)
            try:
                record = parse_document(str(f), image_out_dir=str(output_dir / "images"))
            except PreflightError:
                raise
            except Exception as exc:
                raise PreflightError("corrupt_document", "Document cannot be parsed") from exc
            if not record["blocks"]:
                raise PreflightError("empty_document", "Document contains no extractable content")
            # Normalize native record to block list
            for b in record["blocks"]:
                combined_blocks.append(b)
                if "source_file" not in b:
                    b["source_file"] = f.name
                if "format" not in b:
                    b["format"] = record["format"]

            attachments_meta.append({"file": f.name, "format": record["format"], "pages": None})

        elif ext in EXCEL_EXTS:
            adapter.info("Processing (Excel parser): %s", f.name)
            record = parse_excel(str(f), image_out_dir=str(output_dir / "images"))
            if record.get("_password_required"):
                raise PreflightError("password_required", "Document requires a password")
            if not record["blocks"]:
                raise PreflightError("empty_document", "Spreadsheet contains no extractable content")
            # Normalize record to blocks (same as docx/html)
            for b in record["blocks"]:
                combined_blocks.append(b)
                if "source_file" not in b:
                    b["source_file"] = f.name
                if "format" not in b:
                    b["format"] = record["format"]

            attachments_meta.append({
                "file": f.name,
                "format": record["format"],
                "pages": None,
                "is_password_required": False,
            })

        elif ext in CSV_EXTS:
            adapter.info("Processing (CSV parser): %s", f.name)
            record = parse_csv(str(f), image_out_dir=str(output_dir / "images"))
            if not record["blocks"]:
                raise PreflightError("empty_document", "CSV contains no extractable content")
            for b in record["blocks"]:
                combined_blocks.append(b)
                if "source_file" not in b:
                    b["source_file"] = f.name
                if "format" not in b:
                    b["format"] = record["format"]

            attachments_meta.append({
                "file": f.name,
                "format": record["format"],
                "pages": None,
                "is_password_required": False,
            })

        else:
            raise PreflightError("unsupported_format", "Document format is unsupported")

        if len(combined_blocks) == previous_block_count:
            raise PreflightError("empty_document", "Document contains no extractable content")

    if pp_pipeline is not None:
        clear_gpu_cache()

    # Compute average confidence per file
    file_scores = {}
    for block in combined_blocks:
        conf = block.get("confidence")
        if isinstance(conf, (int, float)) and math.isfinite(conf) and 0.0 <= conf <= 1.0:
            file_scores.setdefault(block.get("source_file"), []).append(conf)

    for att in attachments_meta:
        scores = file_scores.get(att["file"], [])
        att["avg_confidence"] = round(sum(scores) / len(scores), 4) if scores else None

    combined_record = {
        "email_id": email_id,
        "attachments": attachments_meta,
        "blocks": combined_blocks,
    }

    return combined_record
