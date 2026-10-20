"""
Layout Detection & Reading Order Sorting Module
-----------------------------------------------
1. PaddleOCR LayoutDetection (PP-DocLayout_plus-L): Macro layout regions (table, text, title, figure)
2. HuggingFace LayoutReader (LayoutLMv3): Topologically sorts 2D OCR boxes into human reading order on CPU
3. OpenCV Image Processing: Deskewing, morphological rule-line table detection, and padded region cropping
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

import cv2
import numpy as np
import torch
from PIL import Image

log = logging.getLogger("ingest.layout_order")


@dataclass
class LayoutRegion:
    region_id: int
    region_type: str  # 'table', 'text', 'doc_title', 'figure_title', 'figure', etc.
    bbox: List[int]   # [x1, y1, x2, y2]
    confidence: float

    @property
    def width(self) -> int:
        return max(0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> int:
        return max(0, self.bbox[3] - self.bbox[1])


@dataclass
class OrderedOCRText:
    position: int
    text: str
    confidence: float
    bbox: List[int]   # [x1, y1, x2, y2]
    poly: Optional[List[List[int]]] = None


# -------------------------------------------------------------------------
# 1. Models Initialization (Cached Singletons)
# -------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_layout_detector():
    """Lazily load and cache PaddleOCR LayoutDetection (PP-DocLayout_plus-L).
    Consumes only ~600MB VRAM compared to ~5.8GB for full PPStructureV3.
    """
    from paddleocr import LayoutDetection
    log.info("Loading PaddleOCR LayoutDetection (PP-DocLayout_plus-L)...")
    return LayoutDetection()


@lru_cache(maxsize=1)
def get_layoutreader_model():
    """Lazily load HuggingFace hantian/layoutreader model on CPU.
    Uses local cached weights to eliminate remote HTTP network checks.
    """
    configured = os.getenv("LAYOUTREADER_MODEL_DIR")
    if not configured:
        raise RuntimeError("LayoutReader requires a preinstalled local model directory")
    model_dir = Path(configured)
    if not model_dir.is_absolute() or not model_dir.is_dir() or model_dir.is_symlink():
        raise ValueError("LAYOUTREADER_MODEL_DIR must be an existing absolute directory")
    from transformers import LayoutLMv3ForTokenClassification
    log.info("Loading LayoutReader (LayoutLMv3 CPU) from local model directory")
    model = LayoutLMv3ForTokenClassification.from_pretrained(
        str(model_dir), local_files_only=True,
    )
    model.eval()
    return model


# -------------------------------------------------------------------------
# 2. Reading Order Prediction (Fast Spatial <2ms vs LayoutLM Neural)
# -------------------------------------------------------------------------

def fast_spatial_reading_order(
    boxes_xyxy: List[List[int]],
    line_overlap_tol: float = 0.5
) -> List[int]:
    """Blazing-fast 2D spatial topological sort (< 1.5ms on CPU, 0 weights).
    Groups text boxes into reading lines based on vertical overlap,
    and sorts lines top-to-bottom, tokens left-to-right.
    """
    if not boxes_xyxy:
        return []
    if len(boxes_xyxy) == 1:
        return [0]

    indexed = list(enumerate(boxes_xyxy))
    # Initial sort by top y coordinate
    indexed.sort(key=lambda item: (item[1][1], item[1][0]))

    lines: List[List[Tuple[int, List[int]]]] = []
    for idx, box in indexed:
        cy = (box[1] + box[3]) / 2.0
        h = max(1, box[3] - box[1])
        placed = False
        for line in lines:
            line_cy = sum((b[1] + b[3]) / 2.0 for _, b in line) / len(line)
            line_h = sum(b[3] - b[1] for _, b in line) / len(line)
            if abs(cy - line_cy) < max(h, line_h) * line_overlap_tol:
                line.append((idx, box))
                placed = True
                break
        if not placed:
            lines.append([(idx, box)])

    # Sort lines by top-most y, tokens within each line left-to-right by x1
    lines.sort(key=lambda line: min(b[1] for _, b in line))

    # Map original index to sequential rank order
    order_positions = [0] * len(boxes_xyxy)
    current_pos = 0
    for line in lines:
        line.sort(key=lambda item: item[1][0])
        for orig_idx, _ in line:
            order_positions[orig_idx] = current_pos
            current_pos += 1

    return order_positions


def predict_reading_order(
    boxes_xyxy: List[List[int]],
    image_width: int,
    image_height: int,
    strategy: str = "fast_spatial"
) -> List[int]:
    """Sort bounding boxes into sequential human reading order.

    Args:
        boxes_xyxy: List of [x1, y1, x2, y2] pixel coordinates
        image_width: Page width in pixels
        image_height: Page height in pixels
        strategy: 'fast_spatial' (default, ~1ms, 0 MB weights) or 'layoutreader' (neural LayoutLM)

    Returns:
        List of integer positions corresponding to each input box.
    """
    if not boxes_xyxy:
        return []
    if len(boxes_xyxy) == 1:
        return [0]

    if strategy == "fast_spatial":
        return fast_spatial_reading_order(boxes_xyxy)

    # Neural LayoutLM fallback
    from vendor.layoutreader_helpers import boxes2inputs, parse_logits, prepare_inputs

    model = get_layoutreader_model()

    w = max(1, image_width)
    h = max(1, image_height)
    normalized_boxes = []
    for x1, y1, x2, y2 in boxes_xyxy:
        left = max(0, min(1000, int((x1 / w) * 1000)))
        top = max(0, min(1000, int((y1 / h) * 1000)))
        right = max(0, min(1000, int((x2 / w) * 1000)))
        bottom = max(0, min(1000, int((y2 / h) * 1000)))
        normalized_boxes.append([left, top, right, bottom])

    inputs = boxes2inputs(normalized_boxes)
    inputs = prepare_inputs(inputs, model)

    with torch.no_grad():
        logits = model(**inputs).logits.cpu().squeeze(0)

    reading_order = parse_logits(logits, len(normalized_boxes))
    return reading_order


def sort_ocr_regions_reading_order(
    texts: List[str],
    scores: List[float],
    boxes_xyxy: List[List[int]],
    image_width: int,
    image_height: int,
    polys: Optional[List[Any]] = None,
    strategy: str = "fast_spatial"
) -> List[OrderedOCRText]:
    """Combine text lines with predicted reading order and return sorted list."""
    if not texts:
        return []

    order = predict_reading_order(boxes_xyxy, image_width, image_height, strategy=strategy)
    indexed = []
    for i, pos in enumerate(order):
        poly_coords = polys[i].astype(int).tolist() if polys and i < len(polys) else None
        indexed.append((
            pos,
            OrderedOCRText(
                position=pos,
                text=texts[i],
                confidence=float(scores[i]),
                bbox=boxes_xyxy[i],
                poly=poly_coords
            )
        ))

    # Sort strictly by predicted reading order position
    indexed.sort(key=lambda x: x[0])
    return [item[1] for item in indexed]


# -------------------------------------------------------------------------
# 3. Macro Layout Detection (Paddle LayoutDetection)
# -------------------------------------------------------------------------

def detect_layout_regions(image_input: Union[str, Path, np.ndarray]) -> List[LayoutRegion]:
    """Detect macro layout elements (tables, text, titles, figures)."""
    engine = get_layout_detector()

    input_arg = str(image_input) if isinstance(image_input, (str, Path)) else image_input
    results = engine.predict(input_arg)

    regions: List[LayoutRegion] = []
    if not results:
        return regions

    page_data = results[0]
    boxes = page_data.get("boxes", [])

    for i, b in enumerate(boxes):
        coords = [int(v) for v in b["coordinate"]]
        regions.append(LayoutRegion(
            region_id=i,
            region_type=b["label"],
            bbox=coords,
            confidence=float(b["score"])
        ))

    # Sort by vertical top-to-bottom position as initial order
    regions.sort(key=lambda r: (r.bbox[1], r.bbox[0]))
    return regions


# -------------------------------------------------------------------------
# 4. OpenCV Preprocessing & Cropping Utilities
# -------------------------------------------------------------------------

def auto_deskew(image: np.ndarray) -> Tuple[np.ndarray, float]:
    """Straighten tilted document scans using OpenCV minAreaRect."""
    if image is None or image.size == 0:
        return image, 0.0

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]

    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 50:
        return image, 0.0

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    elif angle > 45:
        angle = 90 - angle
    else:
        angle = -angle

    # Ignore micro-rotations
    if abs(angle) < 0.5:
        return image, 0.0

    (h, w) = image.shape[:2]
    center = (w // 2, h // 2)
    m = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(image, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    log.info("Auto-deskewed image by %.2f degrees", angle)
    return rotated, angle


def crop_region_with_padding(
    image: Union[np.ndarray, Image.Image],
    bbox: List[int],
    padding: int = 10
) -> np.ndarray:
    """Crop bounding box with safety padding to prevent clipping borders/descenders."""
    if isinstance(image, Image.Image):
        img_np = np.array(image)
        if len(img_np.shape) == 3 and img_np.shape[2] == 3:
            img_np = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
    else:
        img_np = image

    h, w = img_np.shape[:2]
    x1, y1, x2, y2 = bbox
    pad_x1 = max(0, x1 - padding)
    pad_y1 = max(0, y1 - padding)
    pad_x2 = min(w, x2 + padding)
    pad_y2 = min(h, y2 + padding)

    return img_np[pad_y1:pad_y2, pad_x1:pad_x2]


def detect_ruled_tables_opencv(gray_image: np.ndarray) -> List[List[int]]:
    """Detect ruled grid lines in document using morphological kernels.
    Very fast heuristic table detector (~15ms on CPU).
    """
    thresh = cv2.adaptiveThreshold(
        gray_image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2
    )

    # Detect horizontal lines
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1))
    h_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, h_kernel)

    # Detect vertical lines
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 25))
    v_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, v_kernel)

    # Combine grid
    table_grid = cv2.add(h_lines, v_lines)
    contours, _ = cv2.findContours(table_grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    table_boxes = []
    min_area = (gray_image.shape[0] * gray_image.shape[1]) * 0.02  # At least 2% of page
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w * h >= min_area and w > 100 and h > 50:
            table_boxes.append([x, y, x + w, y + h])

    return table_boxes
