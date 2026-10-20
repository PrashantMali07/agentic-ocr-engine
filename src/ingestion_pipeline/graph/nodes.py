"""
LangGraph Nodes for Hybrid Document Ingestion Workflow
-------------------------------------------------------
Nodes:
1. triage_node: Preflight classification (Digital PDF vs Scanned/Image)
2. preprocess_node: OpenCV deskewing and contrast optimization
3. ocr_node: Pure PaddleOCR text extraction (GPU, ~1.5GB VRAM)
4. reading_order_node: LayoutReader reading order prediction (CPU, 0MB VRAM)
5. table_structuring_node: OpenCV morphology + spatial clustering for HTML tables & headings
6. auditor_node: Verification of confidence, table math, and 0 vs O ambiguities
7. fallback_tesseract_node: Hybrid fallback to Pytesseract if Paddle confidence/coverage is low
8. hitl_gate_node: Native LangGraph interrupt() pause for human review
9. finalize_node: Compiles clean text, HTML tables, and audit metadata
"""

import logging
from pathlib import Path
from typing import Any, List

import cv2
import numpy as np
from langgraph.types import interrupt

from ..core.safe_html import sanitize_tables
from ..extraction.auditor import run_document_audit
from ..extraction.decoupled_pipeline import extract_pure_paddle_ocr
from ..extraction.hybrid import (
    reconcile_token_extractions,
)
from ..extraction.layout_order import (
    auto_deskew,
    sort_ocr_regions_reading_order,
)
from .state import DocumentGraphState
from .table_builder import extract_and_reconstruct_tables

log = logging.getLogger("ingest.graph.nodes")


# -------------------------------------------------------------------------
# Node 1: Triage
# -------------------------------------------------------------------------

def triage_node(state: DocumentGraphState) -> DocumentGraphState:
    """Preflight check: routes digital PDFs directly to CPU parser, loads images for scanned."""
    file_path = state["file_path"]
    p = Path(file_path)
    ext = p.suffix.lower()

    state["format"] = ext.lstrip(".")
    state["fallback_triggered"] = False
    state["hitl_interrupted"] = False

    if ext == ".pdf":
        import pdfplumber

        from ..extraction.pdf import is_page_scanned, parse_digital_pdf_to_blocks, render_pdf_page_to_image

        try:
            with pdfplumber.open(str(p)) as pdf:
                total_pages = len(pdf.pages)
                scanned_pages = {i for i, page in enumerate(pdf.pages, start=1) if is_page_scanned(page)}
        except Exception as e:
            log.warning("pdfplumber open error on %s, treating as scanned: %s", p.name, e)
            total_pages = 1
            scanned_pages = {1}

        state["page_count"] = total_pages

        if len(scanned_pages) == 0:
            log.info("Triage: Pure Digital PDF (%d pages). Routing to CPU pymupdf4llm...", total_pages)
            state["is_digital"] = True
            blocks = parse_digital_pdf_to_blocks(str(p))
            full_text = "\n\n".join(b.get("content", "") for b in blocks)

            # Separate html tables
            tables_html = [b["content"] for b in blocks if b.get("block_label") == "table"]

            state["clean_text"] = full_text
            state["html_content"] = "\n\n".join(tables_html)
            state["engine_used"] = "digital_cpu"
            state["audit"] = {
                "is_valid": True,
                "requires_hitl": False,
                "overall_confidence": 1.0,
                "issues": [],
            }
            return state

        # Scanned PDF: Render page 0
        state["is_digital"] = False
        img_np = render_pdf_page_to_image(str(p), page_idx=0, dpi=220)
        state["image_np"] = img_np
        return state

    if ext in [".html", ".htm"]:
        from bs4 import BeautifulSoup
        log.info("Triage: Pure Digital HTML. Extracting tables and text...")
        state["is_digital"] = True
        state["page_count"] = 1
        with open(str(p), "r", encoding="utf-8", errors="ignore") as f:
            html_raw = f.read()
        soup = BeautifulSoup(html_raw, "html.parser")
        tables_html = [sanitize_tables(str(t)) for t in soup.find_all("table")]
        clean_text = soup.get_text(separator="\n", strip=True)
        state["clean_text"] = clean_text
        state["html_content"] = "\n\n".join(tables_html)
        state["engine_used"] = "digital_html"
        state["audit"] = {
            "is_valid": True,
            "requires_hitl": False,
            "overall_confidence": 1.0,
            "issues": [],
        }
        return state

    # Image file
    state["is_digital"] = False
    state["page_count"] = 1
    img = cv2.imread(str(p))
    if img is None:
        raise FileNotFoundError(f"Could not open image file: {p}")
    state["image_np"] = img
    return state


# -------------------------------------------------------------------------
# Node 2: Preprocess (OpenCV)
# -------------------------------------------------------------------------

def preprocess_node(state: DocumentGraphState) -> DocumentGraphState:
    """Straighten tilted scans, adaptively upscale small screenshots, and pad with white borders."""
    img = state.get("image_np")
    if img is None:
        return state

    deskewed, angle = auto_deskew(img)
    state["image_np"] = deskewed
    state["deskew_angle"] = angle

    h, w = deskewed.shape[:2]
    min_dim = min(h, w)

    # Adaptive scaling & padding:
    # Small screenshots and crops have low character heights (12-16px) and text touching borders.
    # Upscaling to >= 600px minimum dimension and adding 50px white margin ensures DBNet's receptive
    # field reliably captures edge words ('Status', 'Qty', 'Open') and isolated digits ('3', '100').
    if min_dim < 600:
        scale = min(max(1.5, 600.0 / min_dim), 3.5)
        pad = 50
        resized = cv2.resize(deskewed, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        proc_img = cv2.copyMakeBorder(resized, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=[255, 255, 255])
    else:
        scale = 1.0
        pad = 0
        proc_img = deskewed

    state["proc_image_np"] = proc_img
    state["scale"] = scale
    state["pad"] = pad
    return state


# -------------------------------------------------------------------------
# Node 3: Pure PaddleOCR Extraction (GPU, ~1.5GB VRAM) + Hybrid Recovery
# -------------------------------------------------------------------------

def ocr_node(state: DocumentGraphState) -> DocumentGraphState:
    """Extract raw lines, scores, and bounding boxes using Pure PaddleOCR with coordinate inversion
    and hybrid Pytesseract word recovery.
    """
    img = state.get("image_np")
    proc_img = state.get("proc_image_np")
    if img is None:
        return state
    if proc_img is None:
        proc_img = img

    scale = state.get("scale", 1.0)
    pad = state.get("pad", 0)
    h, w = img.shape[:2]

    # 1. Run Pure PaddleOCR on preprocessed (padded + scaled) image
    texts, scores, boxes, polys = extract_pure_paddle_ocr(proc_img)

    # 2. Invert coordinates back to original unpadded, unscaled image coordinate space
    orig_boxes: List[List[int]] = []
    orig_polys: List[Any] = []
    if scale != 1.0 or pad > 0:
        for poly, _box in zip(polys, boxes, strict=False):
            poly_arr = np.array(poly, dtype=float)
            poly_arr[:, 0] = (poly_arr[:, 0] - pad) / scale
            poly_arr[:, 1] = (poly_arr[:, 1] - pad) / scale
            x1 = int(np.clip(np.min(poly_arr[:, 0]), 0, w))
            y1 = int(np.clip(np.min(poly_arr[:, 1]), 0, h))
            x2 = int(np.clip(np.max(poly_arr[:, 0]), 0, w))
            y2 = int(np.clip(np.max(poly_arr[:, 1]), 0, h))
            orig_boxes.append([x1, y1, x2, y2])
            orig_polys.append(poly_arr.astype(int))
    else:
        orig_boxes = boxes
        orig_polys = polys

    # 3. Hybrid Pytesseract reconciliation to recover any single digits (e.g. '3', '100')
    # or edge words that DBNet missed
    merged_texts, merged_scores, merged_boxes, recovered = reconcile_token_extractions(
        paddle_texts=texts,
        paddle_scores=scores,
        paddle_boxes=orig_boxes,
        image=img,
        iou_thresh=0.15,
        max_centroid_dist=50.0
    )

    # For recovered tokens without polygons, construct rectangle polys
    for r in recovered:
        rx1, ry1, rx2, ry2 = r["box"]
        r_poly = np.array([[rx1, ry1], [rx2, ry1], [rx2, ry2], [rx1, ry2]], dtype=int)
        orig_polys.append(r_poly)

    state["raw_texts"] = merged_texts
    state["raw_scores"] = merged_scores
    state["raw_boxes"] = merged_boxes
    state["polys"] = orig_polys
    state["recovered_tokens"] = recovered
    state["engine_used"] = "hybrid_paddle_tesseract" if recovered else "paddle_layoutreader"
    return state


# -------------------------------------------------------------------------
# Node 4: Reading Order Sorting (LayoutReader on CPU)
# -------------------------------------------------------------------------

def reading_order_node(state: DocumentGraphState) -> DocumentGraphState:
    """Sort text boxes into natural sequential reading order using LayoutLMv3."""
    img = state.get("image_np")
    texts = state.get("raw_texts", [])
    scores = state.get("raw_scores", [])
    boxes = state.get("raw_boxes", [])
    polys = state.get("polys", [])

    if img is None or not texts:
        state["ordered_elements"] = []
        return state

    h, w = img.shape[:2]
    ordered_items = sort_ocr_regions_reading_order(
        texts=texts,
        scores=scores,
        boxes_xyxy=boxes,
        image_width=w,
        image_height=h,
        polys=polys
    )

    state["ordered_elements"] = [
        {
            "position": item.position,
            "text": item.text,
            "confidence": round(item.confidence, 3),
            "bbox": item.bbox,
        }
        for item in ordered_items
    ]
    return state


# -------------------------------------------------------------------------
# Node 5: Table & Layout Structuring
# -------------------------------------------------------------------------

def table_structuring_node(state: DocumentGraphState) -> DocumentGraphState:
    """Detect table grids via OpenCV morphology, cluster rows/cols into HTML, and categorize headings."""
    img = state.get("image_np")
    texts = state.get("raw_texts", [])
    scores = state.get("raw_scores", [])
    boxes = state.get("raw_boxes", [])

    if img is None or not texts:
        state["tables"] = []
        state["headings"] = []
        state["body_texts"] = []
        return state

    # 1. Reconstruct tables
    reconstructed_tables, consumed_indices = extract_and_reconstruct_tables(
        image=img,
        texts=texts,
        scores=scores,
        boxes_xyxy=boxes
    )

    tables_data = []
    html_tables_list = []
    for t in reconstructed_tables:
        tables_data.append({
            "table_id": t.table_id,
            "bbox": t.bbox,
            "headers": t.headers,
            "rows": t.rows,
            "rows_dicts": t.rows_dicts,
            "html": t.html,
            "avg_confidence": t.avg_confidence,
        })
        if t.html:
            html_tables_list.append(t.html)

    state["tables"] = tables_data
    state["html_content"] = "\n\n".join(html_tables_list)

    # 2. Categorize remaining text into headings and body
    set(consumed_indices)
    headings = []
    body_texts = []

    # Calculate median line height for heading heuristic
    heights = [b[3] - b[1] for b in boxes if (b[3] - b[1]) > 0]
    median_h = float(np.median(heights)) if heights else 20.0

    ordered = state.get("ordered_elements", [])
    ordered_clean_lines = []

    for item in ordered:
        txt = item["text"]
        bbox = item["bbox"]
        h_line = bbox[3] - bbox[1]

        # Heading heuristic: significantly larger font or all-caps short title
        is_heading = (h_line >= median_h * 1.35) or (txt.isupper() and len(txt) < 60 and not any(c.isdigit() for c in txt))

        if is_heading:
            headings.append(item)
            ordered_clean_lines.append(f"### {txt}")
        else:
            body_texts.append(item)
            ordered_clean_lines.append(txt)

    state["headings"] = headings
    state["body_texts"] = body_texts
    state["clean_text"] = "\n".join(ordered_clean_lines)

    return state


# -------------------------------------------------------------------------
# Node 6: Auditor Agent (Quality Gate)
# -------------------------------------------------------------------------

def auditor_node(state: DocumentGraphState) -> DocumentGraphState:
    """Audit OCR confidence scores, zero vs 'O' ambiguities, and table arithmetic."""
    texts = state.get("raw_texts", [])
    scores = state.get("raw_scores", [])
    tables = state.get("tables", [])

    table_rows_data = [t.get("rows_dicts", []) for t in tables if t.get("rows_dicts")]

    audit_report = run_document_audit(
        texts=texts,
        scores=scores,
        tables_data=table_rows_data,
        min_confidence=0.85
    )

    state["audit"] = audit_report.summary_dict()
    state["hitl_required"] = audit_report.requires_hitl
    return state


# -------------------------------------------------------------------------
# Node 7: Fallback to Pytesseract (Hybrid Recovery)
# -------------------------------------------------------------------------

def fallback_tesseract_node(state: DocumentGraphState) -> DocumentGraphState:
    """Invoked if PaddleOCR confidence is low or text line count is suspiciously low.
    Reconciles missing tokens with Pytesseract and updates tables and reading order.
    """
    img = state.get("image_np")
    if img is None:
        return state

    log.info("Triggering Hybrid Pytesseract Fallback for %s...", state.get("file_path"))
    merged_texts, merged_scores, merged_boxes, recovered = reconcile_token_extractions(
        paddle_texts=state.get("raw_texts", []),
        paddle_scores=state.get("raw_scores", []),
        paddle_boxes=state.get("raw_boxes", []),
        image=img,
        iou_thresh=0.15,
        max_centroid_dist=50.0
    )

    state["raw_texts"] = merged_texts
    state["raw_scores"] = merged_scores
    state["raw_boxes"] = merged_boxes
    state["fallback_triggered"] = True
    state["engine_used"] = "hybrid_paddle_tesseract"

    # Re-order
    h, w = img.shape[:2]
    ordered_items = sort_ocr_regions_reading_order(merged_texts, merged_scores, merged_boxes, w, h)
    state["ordered_elements"] = [
        {"position": item.position, "text": item.text, "confidence": round(item.confidence, 3), "bbox": item.bbox}
        for item in ordered_items
    ]

    # Re-structure tables with newly recovered tokens
    reconstructed_tables, _ = extract_and_reconstruct_tables(
        image=img,
        texts=merged_texts,
        scores=merged_scores,
        boxes_xyxy=merged_boxes
    )
    tables_data = []
    html_tables_list = []
    for t in reconstructed_tables:
        tables_data.append({
            "table_id": t.table_id,
            "bbox": t.bbox,
            "headers": t.headers,
            "rows": t.rows,
            "rows_dicts": t.rows_dicts,
            "html": t.html,
            "avg_confidence": t.avg_confidence,
        })
        if t.html:
            html_tables_list.append(t.html)

    state["tables"] = tables_data
    if html_tables_list:
        state["html_content"] = "\n\n".join(html_tables_list)

    # Re-run audit
    table_rows_data = [t.get("rows_dicts", []) for t in tables_data if t.get("rows_dicts")]
    re_audit = run_document_audit(merged_texts, merged_scores, tables_data=table_rows_data, min_confidence=0.85)
    state["audit"] = re_audit.summary_dict()
    state["hitl_required"] = re_audit.requires_hitl

    return state


# -------------------------------------------------------------------------
# Node 8: Human-in-the-Loop (HITL) Gate
# -------------------------------------------------------------------------

def hitl_gate_node(state: DocumentGraphState) -> DocumentGraphState:
    """If audit requires human review, pauses the LangGraph workflow via interrupt()."""
    if not state.get("hitl_required", False):
        return state

    log.info("HITL Gate: Pausing workflow for human verification")
    state["hitl_interrupted"] = True

    # Native LangGraph interrupt: serializes state and pauses execution
    human_feedback = interrupt({
        "prompt": "Document flagged by Auditor for review.",
        "audit_issues": state.get("audit", {}).get("issues", []),
        "overall_confidence": state.get("audit", {}).get("overall_confidence"),
        "detected_tables_count": len(state.get("tables", [])),
    })

    # When graph is resumed with Command(resume=...), human_feedback is returned
    if human_feedback and isinstance(human_feedback, dict):
        state["human_corrections"] = human_feedback
        if "approved_text" in human_feedback:
            state["clean_text"] = human_feedback["approved_text"]

    return state


# -------------------------------------------------------------------------
# Node 9: Finalize
# -------------------------------------------------------------------------

def finalize_node(state: DocumentGraphState) -> DocumentGraphState:
    """Compiles clean final output payload."""
    state["final_result"] = {
        "status": "success",
        "file_path": state.get("file_path"),
        "format": state.get("format"),
        "pages": state.get("page_count", 1),
        "is_digital": state.get("is_digital", False),
        "engine_used": state.get("engine_used"),
        "fallback_triggered": state.get("fallback_triggered", False),
        "deskew_angle": round(state.get("deskew_angle", 0.0), 2),
        "clean_text": state.get("clean_text", ""),
        "html_content": state.get("html_content", ""),
        "tables": state.get("tables", []),
        "headings_count": len(state.get("headings", [])),
        "raw_boxes": state.get("raw_boxes", []),
        "raw_scores": state.get("raw_scores", []),
        "raw_texts": state.get("raw_texts", []),
        "ordered_elements": state.get("ordered_elements", []),
        "recovered_tokens": state.get("recovered_tokens", []),
        "audit": state.get("audit", {}),
        "human_corrections": state.get("human_corrections"),
    }
    return state
