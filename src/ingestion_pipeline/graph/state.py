"""
LangGraph State Definition for Document Extraction Workflow
------------------------------------------------------------
Defines the shared state dictionary passed across all nodes in the StateGraph.
"""

from typing import Any, Dict, List, Optional, TypedDict

import numpy as np


class DocumentGraphState(TypedDict, total=False):
    # 1. Inputs & Document Metadata
    file_path: str
    format: str
    page_count: int
    is_digital: bool
    image_np: Optional[np.ndarray]
    proc_image_np: Optional[np.ndarray]
    scale: float
    pad: int
    deskew_angle: float

    # 2. OCR Raw Extractions
    raw_texts: List[str]
    raw_scores: List[float]
    raw_boxes: List[List[int]]  # [x1, y1, x2, y2]
    polys: List[Any]
    recovered_tokens: List[Dict[str, Any]]

    # 3. Layout & Reading Order
    ordered_elements: List[Dict[str, Any]]  # Sequential tokens from LayoutReader
    headings: List[Dict[str, Any]]
    body_texts: List[Dict[str, Any]]
    tables: List[Dict[str, Any]]  # Reconstructed table dicts with HTML

    # 4. Assembled Representations
    clean_text: str
    html_content: str

    # 5. Quality Gate & Auditing
    audit: Dict[str, Any]
    engine_used: str  # 'digital_cpu', 'paddle_layoutreader', 'hybrid_paddle_tesseract'
    fallback_triggered: bool

    # 6. Human-In-The-Loop (HITL)
    hitl_required: bool
    hitl_interrupted: bool
    human_corrections: Optional[Dict[str, Any]]

    # 7. Final Output Payload
    final_result: Dict[str, Any]
    errors: List[str]
