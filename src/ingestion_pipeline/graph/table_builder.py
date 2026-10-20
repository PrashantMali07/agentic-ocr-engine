"""
Table Reconstruction & Spatial Clustering Module
------------------------------------------------
1. Detects ruled tables using fast OpenCV morphology (~15ms on CPU, 0 MB GPU VRAM).
2. Spatially associates OCR bounding boxes into detected table boundaries.
3. Clusters bounding boxes into rows and columns by vertical/horizontal coordinates.
4. Generates standard HTML <table> structures and structured row dictionaries.
"""

import logging
from dataclasses import dataclass, field
from html import escape
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger("ingest.table_builder")


@dataclass
class StructuredCell:
    text: str
    confidence: float
    bbox: List[int]  # [x1, y1, x2, y2]
    row_idx: int = 0
    col_idx: int = 0


@dataclass
class ReconstructedTable:
    table_id: int
    bbox: List[int]  # [x1, y1, x2, y2]
    headers: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)
    rows_dicts: List[Dict[str, str]] = field(default_factory=list)
    html: str = ""
    avg_confidence: float = 1.0


def detect_tables_opencv(image: np.ndarray) -> List[List[int]]:
    """Detect bordered table regions using OpenCV morphological kernels."""
    if image is None or image.size == 0:
        return []

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    h, w = gray.shape[:2]

    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2
    )

    # Detect horizontal lines (scale kernel relative to width)
    h_size = max(20, w // 40)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_size, 1))
    h_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, h_kernel)

    # Detect vertical lines (scale kernel relative to height)
    v_size = max(20, h // 40)
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_size))
    v_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, v_kernel)

    # Combine grid
    table_grid = cv2.add(h_lines, v_lines)
    contours, _ = cv2.findContours(table_grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    table_boxes = []
    min_area = (h * w) * 0.015  # At least 1.5% of page area
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw * bh >= min_area and bw > 120 and bh > 50:
            table_boxes.append([x, y, x + bw, y + bh])

    # Sort vertically from top to bottom
    table_boxes.sort(key=lambda b: (b[1], b[0]))
    return table_boxes


def cluster_boxes_into_rows(
    cells: List[StructuredCell],
    y_overlap_threshold: float = 0.5
) -> List[List[StructuredCell]]:
    """Cluster cells into rows based on vertical overlap and y-coordinates."""
    if not cells:
        return []

    # Sort primarily by y_min
    sorted_cells = sorted(cells, key=lambda c: (c.bbox[1], c.bbox[0]))
    rows: List[List[StructuredCell]] = []

    for cell in sorted_cells:
        cy = (cell.bbox[1] + cell.bbox[3]) / 2.0
        ch = max(1, cell.bbox[3] - cell.bbox[1])

        placed = False
        for row in rows:
            # Check row's average vertical bounds
            row_y1 = min(c.bbox[1] for c in row)
            row_y2 = max(c.bbox[3] for c in row)
            row_h = max(1, row_y2 - row_y1)
            row_cy = (row_y1 + row_y2) / 2.0

            # Overlap test: cell center is within row vertical span or substantial overlap
            if abs(cy - row_cy) < max(ch, row_h) * y_overlap_threshold:
                row.append(cell)
                placed = True
                break

        if not placed:
            rows.append([cell])

    # Sort rows by top coordinate, and sort cells within each row left-to-right
    rows.sort(key=lambda r: min(c.bbox[1] for c in r))
    for row in rows:
        row.sort(key=lambda c: c.bbox[0])

    return rows


def build_reconstructed_table(
    table_id: int,
    table_bbox: List[int],
    assigned_cells: List[StructuredCell]
) -> ReconstructedTable:
    """Transform assigned text cells into a ReconstructedTable with HTML and row dicts."""
    if not assigned_cells:
        return ReconstructedTable(table_id=table_id, bbox=table_bbox)

    clustered_rows = cluster_boxes_into_rows(assigned_cells)
    if not clustered_rows:
        return ReconstructedTable(table_id=table_id, bbox=table_bbox)

    # First row is treated as headers if >= 2 columns
    header_cells = clustered_rows[0]
    headers = [c.text.strip() for c in header_cells]
    num_cols = len(headers)

    # Compute column boundary cutoffs using header cell horizontal positions
    col_cuts = []
    if num_cols > 1:
        for i in range(num_cols - 1):
            h_curr_x2 = header_cells[i].bbox[2]
            h_next_x1 = header_cells[i + 1].bbox[0]
            col_cuts.append((h_curr_x2 + h_next_x1) / 2.0)

    def get_column_index(cell: StructuredCell) -> int:
        if not col_cuts:
            return 0
        cx = (cell.bbox[0] + cell.bbox[2]) / 2.0
        for i, cut in enumerate(col_cuts):
            if cx < cut:
                return i
        return len(col_cuts)

    data_rows = []
    row_dicts = []

    for row in clustered_rows[1:]:
        if num_cols > 1:
            row_vals = [""] * num_cols
            for c in row:
                col_i = get_column_index(c)
                if col_i < num_cols:
                    if row_vals[col_i]:
                        row_vals[col_i] += " " + c.text.strip()
                    else:
                        row_vals[col_i] = c.text.strip()
        else:
            row_vals = [c.text.strip() for c in row]

        data_rows.append(row_vals)

        # Create row dict aligned to headers
        r_dict = {}
        for col_idx, val in enumerate(row_vals):
            header_name = headers[col_idx] if col_idx < len(headers) else f"col_{col_idx + 1}"
            r_dict[header_name] = val
        row_dicts.append(r_dict)

    # Reconstruct HTML <table>
    html_parts = ["<table>"]
    if headers:
        html_parts.append("<thead><tr>" + "".join(f"<th>{escape(h)}</th>" for h in headers) + "</tr></thead>")
    html_parts.append("<tbody>")
    for r in data_rows:
        html_parts.append("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in r) + "</tr>")
    html_parts.append("</tbody></table>")
    html_str = "".join(html_parts)

    avg_conf = sum(c.confidence for c in assigned_cells) / len(assigned_cells)

    return ReconstructedTable(
        table_id=table_id,
        bbox=table_bbox,
        headers=headers,
        rows=data_rows,
        rows_dicts=row_dicts,
        html=html_str,
        avg_confidence=round(avg_conf, 3)
    )


def extract_and_reconstruct_tables(
    image: np.ndarray,
    texts: List[str],
    scores: List[float],
    boxes_xyxy: List[List[int]],
    external_table_boxes: Optional[List[List[int]]] = None
) -> Tuple[List[ReconstructedTable], List[int]]:
    """Detect tables and assign OCR tokens to them.

    Returns:
        (tables_list, consumed_box_indices)
    """
    # 1. Use OpenCV morphology if no external boxes provided
    if external_table_boxes:
        table_boxes = external_table_boxes
    else:
        table_boxes = detect_tables_opencv(image)

    reconstructed_tables: List[ReconstructedTable] = []
    consumed_indices = set()

    for t_idx, t_box in enumerate(table_boxes):
        tx1, ty1, tx2, ty2 = t_box
        # Expand slightly by 5px tolerance
        pad_tx1, pad_ty1, pad_tx2, pad_ty2 = tx1 - 5, ty1 - 5, tx2 + 5, ty2 + 5

        table_cells = []
        for i, (txt, score, bbox) in enumerate(zip(texts, scores, boxes_xyxy, strict=False)):
            bx1, by1, bx2, by2 = bbox
            cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0

            if pad_tx1 <= cx <= pad_tx2 and pad_ty1 <= cy <= pad_ty2:
                table_cells.append(StructuredCell(text=txt, confidence=score, bbox=bbox))
                consumed_indices.add(i)

        if table_cells:
            t_obj = build_reconstructed_table(t_idx + 1, t_box, table_cells)
            reconstructed_tables.append(t_obj)

    return reconstructed_tables, sorted(list(consumed_indices))
