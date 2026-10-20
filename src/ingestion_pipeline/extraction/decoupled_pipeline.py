"""
Decoupled High-Performance Document Extraction Pipeline
-------------------------------------------------------
Orchestrates:
1. Preflight check: Digital PDF -> pymupdf4llm (CPU, sub-second)
2. Scanned / Image path:
   a. OpenCV Auto-deskew & normalization
   b. PaddleOCR LayoutDetection (PP-DocLayout_plus-L: ~0.4s, ~600MB VRAM)
   c. Pure PaddleOCR Text Recognition (PP-OCRv6: ~1.8s, ~1.5GB VRAM)
   d. HuggingFace LayoutReader (LayoutLMv3 on CPU: ~0.15s, 0MB VRAM reading order)
   e. OpenCV Padded Table/Region Cropping
   f. Auditor Agent (Confidence, Arithmetic, Ambiguity & HITL Gate)
"""

import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import cv2
import numpy as np

from .auditor import AuditReport, run_document_audit
from .hybrid import reconcile_token_extractions, resolve_ocr_ambiguities
from .layout_order import (
    OrderedOCRText,
    auto_deskew,
    crop_region_with_padding,
    detect_layout_regions,
    sort_ocr_regions_reading_order,
)

log = logging.getLogger("ingest.decoupled")

@lru_cache(maxsize=1)
def get_pure_paddle_ocr():
    """Lazily load and cache the singleton Pure PaddleOCR engine."""
    from paddleocr import PaddleOCR
    return PaddleOCR(use_textline_orientation=True, lang="en")


def extract_pure_paddle_ocr(image_np: np.ndarray) -> Tuple[List[str], List[float], List[List[int]], List[Any]]:
    """Extract text lines, confidence scores, and bounding boxes via Pure PaddleOCR.
    Consumes ~1.5 GB VRAM (vs ~5.8 GB for PPStructureV3).
    """
    ocr = get_pure_paddle_ocr()
    results = ocr.predict(image_np)

    texts: List[str] = []
    scores: List[float] = []
    boxes_xyxy: List[List[int]] = []
    polys: List[Any] = []

    if not results:
        return texts, scores, boxes_xyxy, polys

    page = results[0]
    rec_texts = page.get("rec_texts", [])
    rec_scores = page.get("rec_scores", [])
    rec_polys = page.get("rec_polys", [])

    for txt, score, poly in zip(rec_texts, rec_scores, rec_polys, strict=False):
        poly_arr = np.array(poly, dtype=int)
        x_min = int(np.min(poly_arr[:, 0]))
        y_min = int(np.min(poly_arr[:, 1]))
        x_max = int(np.max(poly_arr[:, 0]))
        y_max = int(np.max(poly_arr[:, 1]))

        # Disambiguate 0 vs O in numeric contexts
        cleaned_txt = resolve_ocr_ambiguities(txt)

        texts.append(cleaned_txt)
        scores.append(float(score))
        boxes_xyxy.append([x_min, y_min, x_max, y_max])
        polys.append(poly_arr)

    return texts, scores, boxes_xyxy, polys


def process_scanned_image_decoupled(
    image_input: Union[str, Path, np.ndarray],
    deskew: bool = True,
    min_audit_confidence: float = 0.85
) -> Dict[str, Any]:
    """Process a single scanned image or document page through the decoupled pipeline."""
    t0 = time.time()

    # 1. Load image as numpy array
    if isinstance(image_input, (str, Path)):
        img_path = str(image_input)
        img = cv2.imread(img_path)
        if img is None:
            raise FileNotFoundError(f"Could not load image: {image_input}")
    else:
        img = image_input

    # 2. OpenCV Deskewing
    deskew_angle = 0.0
    if deskew:
        img, deskew_angle = auto_deskew(img)

    h, w = img.shape[:2]

    # 3. Macro Layout Detection (PP-DocLayout_plus-L)
    t_layout_start = time.time()
    layout_regions = detect_layout_regions(img)
    t_layout = round(time.time() - t_layout_start, 3)

    # 4. Pure PaddleOCR Text Extraction with Adaptive Preprocessing & Hybrid Recovery
    t_ocr_start = time.time()
    min_dim = min(h, w)
    if min_dim < 600:
        scale = min(max(1.5, 600.0 / min_dim), 3.5)
        pad = 50
        resized = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        proc_img = cv2.copyMakeBorder(resized, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=[255, 255, 255])
    else:
        scale = 1.0
        pad = 0
        proc_img = img

    texts, scores, boxes_xyxy, polys = extract_pure_paddle_ocr(proc_img)

    if scale != 1.0 or pad > 0:
        inv_boxes = []
        inv_polys = []
        for poly in polys:
            poly_arr = np.array(poly, dtype=float)
            poly_arr[:, 0] = (poly_arr[:, 0] - pad) / scale
            poly_arr[:, 1] = (poly_arr[:, 1] - pad) / scale
            x1 = int(np.clip(np.min(poly_arr[:, 0]), 0, w))
            y1 = int(np.clip(np.min(poly_arr[:, 1]), 0, h))
            x2 = int(np.clip(np.max(poly_arr[:, 0]), 0, w))
            y2 = int(np.clip(np.max(poly_arr[:, 1]), 0, h))
            inv_boxes.append([x1, y1, x2, y2])
            inv_polys.append(poly_arr.astype(int))
        boxes_xyxy = inv_boxes
        polys = inv_polys

    # Hybrid Tesseract recovery for any tokens missed on borders or table cells
    texts, scores, boxes_xyxy, recovered = reconcile_token_extractions(
        paddle_texts=texts,
        paddle_scores=scores,
        paddle_boxes=boxes_xyxy,
        image=img,
        iou_thresh=0.15,
        max_centroid_dist=50.0
    )
    for r in recovered:
        rx1, ry1, rx2, ry2 = r["box"]
        polys.append(np.array([[rx1, ry1], [rx2, ry1], [rx2, ry2], [rx1, ry2]], dtype=int))

    t_ocr = round(time.time() - t_ocr_start, 3)

    # 5. Reading Order Sorting via LayoutReader (LayoutLMv3 CPU)
    t_order_start = time.time()
    ordered_regions: List[OrderedOCRText] = sort_ocr_regions_reading_order(
        texts=texts,
        scores=scores,
        boxes_xyxy=boxes_xyxy,
        image_width=w,
        image_height=h,
        polys=polys
    )
    t_order = round(time.time() - t_order_start, 3)

    # 6. OpenCV Cropping of Tables and Figures
    table_regions = [r for r in layout_regions if r.region_type == "table"]
    figure_regions = [r for r in layout_regions if r.region_type in ["figure", "chart"]]

    table_crops = []
    for r in table_regions:
        crop_img = crop_region_with_padding(img, r.bbox, padding=12)
        table_crops.append({
            "region_id": r.region_id,
            "bbox": r.bbox,
            "confidence": r.confidence,
            "crop_shape": list(crop_img.shape),
        })

    # Assemble full ordered text
    ordered_lines = [item.text for item in ordered_regions]
    full_text = "\n".join(ordered_lines)

    # 7. Auditor Agent Quality & HITL Check
    audit_report: AuditReport = run_document_audit(
        texts=ordered_lines,
        scores=scores,
        min_confidence=min_audit_confidence
    )

    t_total = round(time.time() - t0, 3)

    return {
        "status": "success",
        "processing_mode": "decoupled_gpu_cpu_pipeline",
        "timings_sec": {
            "layout_detection": t_layout,
            "pure_ocr": t_ocr,
            "layoutreader_ordering": t_order,
            "total_elapsed": t_total,
        },
        "page_dimensions": {"width": w, "height": h},
        "deskew_angle": deskew_angle,
        "texts_count": len(texts),
        "tables_detected": len(table_regions),
        "figures_detected": len(figure_regions),
        "table_crops_metadata": table_crops,
        "full_text": full_text,
        "ordered_text_elements": [
            {
                "position": item.position,
                "text": item.text,
                "confidence": round(item.confidence, 3),
                "bbox": item.bbox,
            }
            for item in ordered_regions
        ],
        "layout_regions": [
            {
                "region_id": r.region_id,
                "type": r.region_type,
                "confidence": round(r.confidence, 3),
                "bbox": r.bbox,
            }
            for r in layout_regions
        ],
        "audit": audit_report.summary_dict(),
    }


def process_document_decoupled(
    file_path: Union[str, Path],
    deskew: bool = True,
    min_audit_confidence: float = 0.85
) -> Dict[str, Any]:
    """Unified entrypoint: intelligently routes digital PDFs vs scanned pages."""
    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = p.suffix.lower()

    # 1. Digital PDF Routing (Zero GPU VRAM, < 0.5s)
    if suffix == ".pdf":
        import pdfplumber

        from .pdf import is_page_scanned, parse_digital_pdf_to_blocks, render_pdf_page_to_image
        with pdfplumber.open(str(p)) as pdf:
            total_pages = len(pdf.pages)
            scanned_pages = {i for i, page in enumerate(pdf.pages, start=1) if is_page_scanned(page)}

        if len(scanned_pages) == 0:
            log.info("Detected pure digital PDF (%d pages): routing to CPU pymupdf4llm...", total_pages)
            t0 = time.time()
            blocks = parse_digital_pdf_to_blocks(str(p))
            full_text = "\n\n".join(b.get("content", "") for b in blocks)
            t_total = round(time.time() - t0, 3)
            return {
                "status": "success",
                "processing_mode": "digital_pdf_cpu",
                "timings_sec": {"total_elapsed": t_total},
                "pages": total_pages,
                "full_text": full_text,
                "average_confidence": 1.0,
                "audit": {
                    "is_valid": True,
                    "requires_hitl": False,
                    "overall_confidence": 1.0,
                    "issues": [],
                }
            }

        # Scanned PDF: Render page 0 and process
        img_np = render_pdf_page_to_image(str(p), page_idx=0, dpi=220)
        return process_scanned_image_decoupled(img_np, deskew=deskew, min_audit_confidence=min_audit_confidence)

    # 2. Image Files (.jpg, .png, .tiff, etc.)
    return process_scanned_image_decoupled(p, deskew=deskew, min_audit_confidence=min_audit_confidence)
