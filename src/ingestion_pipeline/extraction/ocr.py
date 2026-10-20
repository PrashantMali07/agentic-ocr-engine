import io
import logging
import threading
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import paddle
import pandas as pd

from ..config import (
    LAYOUT_MERGE_BBOXES_MODE,
    LAYOUT_NMS,
    LAYOUT_UNCLIP_RATIO,
    OCR_DTYPE,
    OCR_ENHANCE,
    OCR_GRAYSCALE,
    OCR_MAX_DIM,
    PADDLE_USE_GPU,
    TEXT_DET_BOX_THRES,
    TEXT_DET_LIMIT_SIDE_LEN_IMAGE,
    TEXT_DET_LIMIT_TYPE,
    TEXT_DET_THRES,
    TEXT_DET_UNCLIP_RATIO,
    TEXT_REC_SCORE_THRES,
    USE_DOC_ORIENTATION_CLASSIFY,
    USE_TABLE_RECOGNITION,
)
from ..core.safe_html import sanitize_tables

log = logging.getLogger("ingest.ocr")

# get_ocr_pipeline() below is @lru_cache(maxsize=1) - a single PPStructureV3
# instance shared by every caller in this process. PaddleInference
# predictors are not safe for concurrent .predict() calls on the same
# instance: two threads calling in at once can interleave internal
# tensors mid-graph and raise shape-mismatch errors, e.g.:
#   InvalidArgumentError: The 0-th dimension of input[0] and input[3] is
#   expected to be equal...
# This became reachable once extraction started running via
# asyncio.to_thread with more than one concurrent job. All callers MUST
# go through ocr_predict() below instead of calling pipeline.predict()
# directly, so access to the shared pipeline is always serialized
# regardless of which endpoint or thread triggered it.
_pipeline_lock = threading.Lock()


def ocr_predict(pipeline, input_data) -> list:
    """Thread-safe wrapper around PPStructureV3.predict(). Always use this
    instead of calling pipeline.predict() directly - see note above."""
    with _pipeline_lock:
        return list(pipeline.predict(input=input_data))



@lru_cache(maxsize=1)
def get_ocr_pipeline(
    text_det_unclip_ratio: float = TEXT_DET_UNCLIP_RATIO,
    text_det_thresh: float = TEXT_DET_THRES,
    text_rec_score_thresh: float = TEXT_REC_SCORE_THRES,
    use_large_pdf_settings: bool = False,
):
    """Lazily import and construct the singleton PP-StructureV3 pipeline.

    IMPORTANT FOR GPU VRAM (12 GB target):
    Cached with maxsize=1 to ensure ONLY ONE model instance is ever loaded into
    GPU VRAM at any time. PP-StructureV3 holds 10+ neural network predictors
    which together consume ~5.5-6 GB VRAM. Keeping multiple instances alive
    simultaneously would exceed 11-12 GB and trigger CUDA Out-of-Memory (OOM).

    The unified settings (layout_unclip_ratio=1.05, det_unclip_ratio=1.6)
    consistently process both dense PDFs and small image crops without
    requiring multiple conflicting model instances.
    """
    from paddleocr import PPStructureV3

    # PaddleX otherwise selects GPU whenever one is visible, even when the
    # service's PADDLE_USE_GPU setting is false.
    device = "gpu:0" if PADDLE_USE_GPU else "cpu"
    paddle.set_device(device)

    effective_text_det_limit_side_len = TEXT_DET_LIMIT_SIDE_LEN_IMAGE
    # Keep layout unclip ratio constrained to avoid layout bounding boxes swallowing nearby text
    effective_layout_unclip_ratio = float(min(LAYOUT_UNCLIP_RATIO, 1.1))
    effective_text_det_box_thresh = float(TEXT_DET_BOX_THRES)
    effective_text_det_unclip_ratio = float(text_det_unclip_ratio)
    effective_text_rec_score_thres = float(text_rec_score_thresh)
    effective_use_doc_unwrapping = False

    log.info(
        f"Loading PP-StructureV3 (unclip={effective_text_det_unclip_ratio}, "
        f"layout_unclip={effective_layout_unclip_ratio}, "
        f"det_thr={text_det_thresh}, rec_thr={effective_text_rec_score_thres}, device={device})"
    )
    from ..model_artifacts import configured_model_directories

    return PPStructureV3(
        device=device,
        layout_nms=LAYOUT_NMS,
        layout_unclip_ratio=effective_layout_unclip_ratio,
        layout_merge_bboxes_mode="union" if LAYOUT_MERGE_BBOXES_MODE == "large" else LAYOUT_MERGE_BBOXES_MODE,
        text_det_thresh=text_det_thresh,
        text_det_box_thresh=effective_text_det_box_thresh,
        text_det_unclip_ratio=effective_text_det_unclip_ratio,
        text_det_limit_side_len=effective_text_det_limit_side_len,
        text_det_limit_type=TEXT_DET_LIMIT_TYPE,
        text_rec_score_thresh=effective_text_rec_score_thres,
        use_doc_orientation_classify=USE_DOC_ORIENTATION_CLASSIFY,
        use_doc_unwarping=effective_use_doc_unwrapping,
        use_table_recognition=USE_TABLE_RECOGNITION,
        **configured_model_directories(),
    )

def is_low_contrast_image(img: np.ndarray) -> bool:
    """Heuristic to determine if an image has low dynamic range or faint text."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    p5, p95 = np.percentile(gray, (5, 95))
    return (p95 - p5) < 70

def prepare_image_for_ocr(image_path: str, is_large_scan: bool = False) -> np.ndarray:
    """Load an image, optionally scale, enhance, and cast dtype.

    The returned ndarray is suitable for 'PPStructureV3.predict' which accepts a
    NumPy array (BGR channel order). All processing respects the global config
    values 'OCR_MAX_DIM', 'OCR_DTYPE' and 'OCR_GRAYSCALE'.
    """
    from PIL import Image

    from ..processing.preflight import MAX_IMAGE_PIXELS, MAX_IMAGE_SIDE

    with Image.open(image_path) as header:
        width, height = header.size
        if (width < 1 or height < 1 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE
                or width * height > MAX_IMAGE_PIXELS):
            raise ValueError("Image dimensions exceed the supported limit")
    img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Unable to read image: {image_path}")

    h, w = img.shape[:2]
    min_dim = min(h, w)
    max_dim = max(h, w)

    # Upscale only small images where characters might be tiny
    if min_dim < 600:
        scale = min(max(1.5, 900 / min_dim), 3.0,
                    4000 / max_dim, (MAX_IMAGE_PIXELS / (w * h)) ** 0.5)
        if scale <= 1:
            scale = 1
        log.info(
            "Small image (%dx%d). Upscaling by %.2fx for improved character detection.",
            w, h, scale
        )
        if scale > 1:
            img = cv2.resize(img, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        h, w = img.shape[:2]
        max_dim = max(h, w)

    # Down-scale uniformly if any side exceeds OCR_MAX_DIM (checked unconditionally)
    if max_dim > min(OCR_MAX_DIM, 4000):
        scale = min(OCR_MAX_DIM, 4000) / max_dim
        log.info("Large image (%dx%d). Downscaling by %.2fx to fit OCR_MAX_DIM=%d.", w, h, scale, OCR_MAX_DIM)
        img = cv2.resize(img, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    # Contrast enhancement: only when enabled or for genuine low-contrast scans
    if OCR_ENHANCE or is_low_contrast_image(img):
        log.info("Applying CLAHE contrast enhancement for low-contrast document.")
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        # LAB channels: L = lightness, a/b = color axes.
        lightness, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8))
        l_enhanced = clahe.apply(lightness)
        img = cv2.cvtColor(cv2.merge((l_enhanced, a, b)), cv2.COLOR_LAB2BGR)

    # Optional grayscale conversion
    if OCR_GRAYSCALE:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    # Cast to requested dtype
    target_dtype = "float32" if OCR_DTYPE == "float16" else OCR_DTYPE
    if target_dtype != "uint8":
        try:
            img = img.astype(target_dtype)
        except TypeError as e:
            log.warning("Failed to cast image to dtype %s: %s", target_dtype, e)
        img = img.astype(np.uint8)

    # Border padding: 15px white border avoids edge clipping without coordinate distortion
    img = cv2.copyMakeBorder(
        img,
        top=15, bottom=15, left=15, right=15,
        borderType=cv2.BORDER_CONSTANT,
        value=[255, 255, 255]
    )

    return img

def get_res_root(json_data: dict) -> dict:
    """PP-StructureV3's res.json is sometimes flat, sometimes wrapped in a
    top-level 'res' key. Normalize so downstream code always reads the actual content dict."""
    if isinstance(json_data, dict) and isinstance(json_data.get("res"), dict):
        return json_data["res"]
    return json_data if isinstance(json_data, dict) else {}

def pp_structure_to_blocks(res, source_file: str, page_num: int) -> list[dict]:
    """Parse a PaddleOCR result into the unified block schema."""
    data = res.json
    root = get_res_root(data)

    parsing_blocks = root.get("parsing_res_list", [])
    tables = root.get("table_res_list", [])

    page_fallback_conf = None
    ocr_res = root.get("overall_ocr_res", {})
    if isinstance(ocr_res, dict):
        rec_scores = ocr_res.get("rec_scores", [])
        if isinstance(rec_scores, list) and len(rec_scores) > 0:
            valid_scores = [s for s in rec_scores if isinstance(s, (int, float))]
            if valid_scores:
                page_fallback_conf = sum(valid_scores) / len(valid_scores)

    blocks = []
    table_ptr = 0
    for i, b in enumerate(parsing_blocks):
        if not isinstance(b, dict):
            continue
        label = b.get("block_label", "text")
        if label == "table":
            if table_ptr < len(tables):
                content = tables[table_ptr].get("pred_html", b.get("block_content", ""))
            else:
                content = b.get("block_content", "")
            table_ptr += 1
            content = sanitize_tables(str(content))
        else:
            content = b.get("block_content", "")
            if isinstance(content, dict):
                content = content.get("text") or content.get("content") or str(content)
            elif isinstance(content, list):
                content = "\n".join(
                    (item.get("text") if isinstance(item, dict) else str(item)) for item in content
                )

        # Do not discard meaningful text inside figure or image blocks
        if label in {"image", "figure"} and not str(content).strip():
            continue

        confidence = b.get("block_score", b.get("score"))
        if confidence is None:
            confidence = page_fallback_conf

        blocks.append({
            "block_label": label,
            "content": content,
            "order": i,
            "page": page_num,
            "confidence": round(float(confidence), 4) if isinstance(confidence, (int, float)) else None,
            "bbox": b.get("block_bbox"),
            "source_file": source_file,
            "format": Path(source_file).suffix.lstrip(".").lower(),
        })
    return blocks

def extract_tables_to_csv(res, out_dir: Path, doc_name: str, page_num: int) -> list[str]:
    """Pull HTML tables out of a PP-StructureV3 result and save each as CSV."""
    csv_paths = []
    try:
        json_data = res.json
    except Exception:
        json_data = None

    root = get_res_root(json_data)
    tables = root.get("table_res_list", [])

    for table_idx, tbl in enumerate(tables if isinstance(tables, list) else [], start=1):
        html = tbl.get("pred_html") if isinstance(tbl, dict) else None
        if not html:
            continue
        try:
            dfs = pd.read_html(io.StringIO(str(html)))
        except Exception as e:
            log.warning("Could not parse table %d on page %d as HTML: %s", table_idx, page_num, e)
            continue
        for i, df in enumerate(dfs):
            csv_name = f"{doc_name}_p{page_num}_table{table_idx}" + (f"_{i}" if len(dfs) > 1 else "") + ".csv"
            csv_path = out_dir / csv_name
            df.to_csv(csv_path, index=False)
            csv_paths.append(str(csv_path))
    return csv_paths

def clear_gpu_cache():
    """Clear CUDA cache to prevent OOM during large batch processing."""
    if PADDLE_USE_GPU:
        try:
            paddle.device.cuda.empty_cache()
        except Exception as e:
            log.warning("Failed to clear CUDA cache: %s", e)
