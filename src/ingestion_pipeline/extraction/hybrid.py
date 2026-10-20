import logging
import re
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
import pytesseract
from PIL import Image

log = logging.getLogger("ingest.hybrid")

# Common English words where '0' is almost certainly a misrecognized 'O'
COMMON_ALPHA_WORDS = {
    "ORDER", "OPEN", "NO", "FOR", "TO", "CUSTOMER", "CARGO", "ORIGIN",
    "DESTINATION", "PRODUCT", "DETAILS", "TOTAL", "DOCUMENT", "INVOICE",
    "RECEIPT", "PAYMENT", "ACCOUNT", "AIRPORT", "FREIGHT", "PORT",
    "COMPANY", "ADDRESS", "STATE", "COUNTRY", "CONTACT", "EMAIL",
    "NAME", "DATE", "VALIDITY", "ROUTE", "TERMS", "DESCRIPTION", "UNIT",
    "VALUE", "PACKAGING", "SPECIAL", "INSTRUCTIONS", "CONFIRMED", "BOOKING",
    "PURPOSES", "DEMONSTRATION", "NORTHSTAR", "OCEANIC", "FALCON"
}


def resolve_ocr_ambiguities(text: str) -> str:
    """Context-aware resolution of OCR character ambiguities (especially 0 vs O / o).

    Rules applied:
    1. Numeric contexts (phone numbers, pin codes, dates, currency, quantities):
       Misrecognized 'O' or 'o' is converted to '0'.
    2. Alphabetic contexts (common words, uppercase dictionary matches):
       Misrecognized '0' is converted to 'O'.
    3. Alphanumeric codes & identifiers (e.g. RFQ/2026/1006):
       Year-like and numeric sequence segments resolve 'O' -> '0'.
    """
    if not text:
        return text

    # --- 1. NUMERIC FIXES (O/o -> 0) ---

    # Currency values: e.g. $159.Oo -> $159.00, 287,571.4O -> 287,571.40
    text = re.sub(r'(\$\s*\d+[\.,]\d*)[Oo]+', r'\g<1>0', text)
    text = re.sub(r'(\b\d+[\.,]\d*)[Oo]+', r'\g<1>0', text)

    # Isolated 'O' or 'o' between digits: e.g. 2O26 -> 2026, 1O0 -> 100, 9O374 -> 90374
    text = re.sub(r'(?<=\d)[Oo]+(?=\d)', lambda m: '0' * len(m.group(0)), text)

    # 'O' followed by digits at word boundary or after punctuation: e.g. /2O26 -> /2026, -9O37 -> -9037
    text = re.sub(r'(?<=[\s\-\/\:\.\,\(])[Oo]+(?=\d{2,})', lambda m: '0' * len(m.group(0)), text)

    # Digits followed by 'O' or 'o' at end of numeric token: e.g. 100O -> 1000, 1OO6 -> 1006
    text = re.sub(r'(\d{2,})[Oo]+(?=[\s\-\/\:\.\,\)\b]|$)', lambda m: m.group(1) + ('0' * (len(m.group(0)) - len(m.group(1)))), text)

    # Dates: e.g. 24-Jan-2O26, 2O26/05/26
    def fix_date(m):
        return m.group(1) + m.group(2).replace('O', '0').replace('o', '0')
    text = re.sub(r'(\b\d{1,2}[\-\/][A-Za-z]{3}[\-\/])([0-9Oo]{4}\b)', fix_date, text)
    text = re.sub(r'(\b)([0-9Oo]{4})([\-\/]\d{1,2}[\-\/]\d{1,2}\b)', lambda m: m.group(1) + m.group(2).replace('O', '0').replace('o', '0') + m.group(3), text)

    # Phone numbers (+91-9O3743O528, 8882427257)
    def fix_phone(m):
        token = m.group(0)
        # only replace if token has enough digits to be a phone number
        digits = sum(c.isdigit() for c in token)
        if digits >= 7:
            return token.replace('O', '0').replace('o', '0')
        return token
    text = re.sub(r'(\+?\d[\d\s\-\(\)Oo]{7,}\d)', fix_phone, text)

    # Indian Pincodes (6 digits, e.g. 2O8OO1, 110077, 600001, 411001)
    def fix_pincode(m):
        token = m.group(0)
        return token.replace('O', '0').replace('o', '0')
    text = re.sub(r'\b[1-9][0-9Oo]{5}\b', fix_pincode, text)

    # Dimensions and quantities: e.g. 23Omx12m -> 230mx12m, 1O0 PCS -> 100 PCS, 999 SETS
    text = re.sub(r'(\b\d+)[Oo]+(m|cm|mm|kg|pcs|sets|in|inches)\b', r'\g<1>0\g<2>', text, flags=re.IGNORECASE)

    # Common code patterns like RFQ/2026/1006
    def fix_code(m):
        prefix = m.group(1)
        suffix = m.group(2).replace('O', '0').replace('o', '0')
        return prefix + suffix
    text = re.sub(r'\b([A-Za-z]{2,}[\-\/])([0-9Oo\-\/]+)\b', fix_code, text)

    # Email domains and URLs: e.g. .c0m -> .com, www.ind0l0g -> www.indolog
    text = re.sub(r'\.c0m\b', '.com', text, flags=re.IGNORECASE)
    text = re.sub(r'\b0rg\b', 'org', text, flags=re.IGNORECASE)

    # --- 2. ALPHABETIC FIXES (0 -> O) ---

    def fix_word_zeros(m):
        word = m.group(0)
        upper_word = word.upper().replace('0', 'O')
        if upper_word in COMMON_ALPHA_WORDS:
            # Reconstruct with case preserved
            res = []
            for orig, _fixed in zip(word, upper_word, strict=False):
                if orig == '0':
                    res.append('O' if orig.isupper() or word.isupper() else 'o')
                else:
                    res.append(orig)
            return "".join(res)
        # If word is at least 3 letters, all other chars are letters, and single 0 is enclosed
        if len(word) >= 3 and word.count('0') == 1:
            letters_count = sum(c.isalpha() for c in word)
            if letters_count == len(word) - 1:
                return word.replace('0', 'O' if word.isupper() else 'o')
        return word

    text = re.sub(r'\b[A-Za-z0-9]+\b', fix_word_zeros, text)

    return text


def extract_tesseract_lines(img: np.ndarray) -> List[Dict[str, Any]]:
    """Extract line-level text, bounding boxes, and confidences using Tesseract.

    Accepts either an OpenCV BGR ndarray or a PIL Image.
    Returns a list of dicts:
        [{
            'text': str,
            'bbox': (x1, y1, x2, y2),
            'confidence': float (0.0 to 1.0)
        }]
    """
    if isinstance(img, np.ndarray):
        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    else:
        pil_img = img

    try:
        data = pytesseract.image_to_data(pil_img, output_type=pytesseract.Output.DICT, timeout=30)
    except Exception as e:
        log.warning("Pytesseract extraction failed (%s)", type(e).__name__)
        return []

    lines: List[List[Tuple[str, Tuple[int, int, int, int], int]]] = []
    current_line: List[Tuple[str, Tuple[int, int, int, int], int]] = []
    last_line_id = None

    n_boxes = len(data.get("text", []))
    for i in range(n_boxes):
        word = data["text"][i].strip()
        conf = int(data["conf"][i]) if "conf" in data else -1
        if not word or conf < 10:
            continue

        line_id = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]

        if line_id != last_line_id and current_line:
            lines.append(current_line)
            current_line = []
        last_line_id = line_id
        current_line.append((word, (x, y, x + w, y + h), conf))

    if current_line:
        lines.append(current_line)

    formatted_lines: List[Dict[str, Any]] = []
    for line in lines:
        text = " ".join(w[0] for w in line)
        box = (
            min(w[1][0] for w in line),
            min(w[1][1] for w in line),
            max(w[1][2] for w in line),
            max(w[1][3] for w in line),
        )
        avg_conf = sum(max(0, w[2]) for w in line) / (len(line) * 100.0)
        formatted_lines.append({
            "text": text,
            "bbox": box,
            "confidence": round(avg_conf, 4)
        })

    return formatted_lines


def extract_tesseract_words(img: np.ndarray, min_conf: float = 30.0) -> List[Dict[str, Any]]:
    """Extract word-level text tokens, bounding boxes, and confidences using Tesseract.
    Returns:
        [{'text': str, 'confidence': float (0.0 to 1.0), 'box': [x1, y1, x2, y2]}]
    """
    if isinstance(img, np.ndarray):
        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    else:
        pil_img = img

    try:
        data = pytesseract.image_to_data(pil_img, output_type=pytesseract.Output.DICT, timeout=30)
    except Exception as e:
        log.warning("Pytesseract word extraction failed (%s)", type(e).__name__)
        return []

    words: List[Dict[str, Any]] = []
    n = len(data.get("text", []))
    for i in range(n):
        txt = data["text"][i].strip()
        conf = float(data["conf"][i]) if "conf" in data else -1.0
        if txt and conf >= min_conf:
            cleaned_txt = txt
            x = int(data["left"][i])
            y = int(data["top"][i])
            w = int(data["width"][i])
            h = int(data["height"][i])
            words.append({
                "text": cleaned_txt,
                "confidence": round(conf / 100.0, 3),
                "box": [x, y, x + w, y + h]
            })
    return words


def _boxes_overlap(box_a, box_b, iou_threshold: float = 0.2) -> bool:
    """True if box_a overlaps box_b by more than the specified area fraction."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)

    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return False

    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)

    if min(area_a, area_b) <= 0:
        return False

    # Overlap relative to the smaller box
    overlap_ratio = inter_area / float(min(area_a, area_b))
    return overlap_ratio >= iou_threshold


def reconcile_token_extractions(
    paddle_texts: List[str],
    paddle_scores: List[float],
    paddle_boxes: List[List[int]],
    image: np.ndarray,
    iou_thresh: float = 0.15,
    max_centroid_dist: float = 50.0
) -> Tuple[List[str], List[float], List[List[int]], List[Dict[str, Any]]]:
    """Reconciles PaddleOCR extractions with Tesseract word tokens.

    Any word or quantity detected by Tesseract that was missed by PaddleOCR
    (e.g. edge characters, isolated digits like '3' or '100' in table cells)
    is merged into the extraction token pool.

    Returns:
        (merged_texts, merged_scores, merged_boxes, recovered_tokens)
    """
    tess_words = extract_tesseract_words(image)
    if not tess_words:
        return paddle_texts, paddle_scores, paddle_boxes, []

    paddle_tokens = []
    for txt, score, box in zip(paddle_texts, paddle_scores, paddle_boxes, strict=False):
        paddle_tokens.append({"text": txt, "score": score, "box": box})

    recovered = []
    for tw in tess_words:
        tx1, ty1, tx2, ty2 = tw["box"]
        tcx, tcy = (tx1 + tx2) / 2.0, (ty1 + ty2) / 2.0
        tw_txt = tw["text"].strip().lower()

        is_covered = False
        for pt in paddle_tokens:
            px1, py1, px2, py2 = pt["box"]
            pt_txt = pt["text"].strip().lower()

            # 1. Bounding box intersection overlap
            if _boxes_overlap(tw["box"], pt["box"], iou_threshold=iou_thresh):
                is_covered = True
                break

            # 2. Text match + centroid proximity (for cases where box coords slightly deviate)
            if (tw_txt == pt_txt or tw_txt in pt_txt) and abs(tcx - (px1 + px2) / 2.0) < max_centroid_dist and abs(tcy - (py1 + py2) / 2.0) < max_centroid_dist:
                is_covered = True
                break

        if not is_covered:
            recovered.append(tw)

    merged_texts = list(paddle_texts)
    merged_scores = list(paddle_scores)
    merged_boxes = list(paddle_boxes)

    for r in recovered:
        merged_texts.append(r["text"])
        merged_scores.append(r["confidence"])
        merged_boxes.append(r["box"])

    if recovered:
        log.info(
            "Hybrid Tesseract recovery added %d missed tokens",
            len(recovered)
        )

    return merged_texts, merged_scores, merged_boxes, recovered


def _suggest_correction(block: Dict[str, Any]) -> None:
    if block.get("block_label") not in {"text", "heading"}:
        return
    original = block.get("content")
    if not isinstance(original, str):
        return
    proposed = resolve_ocr_ambiguities(original)
    if proposed != original:
        block["suggested_content"] = proposed


def reconcile_paddle_and_tesseract(
    paddle_blocks: List[Dict[str, Any]],
    tesseract_lines: List[Dict[str, Any]],
    source_file: str,
    page_num: int,
    file_format: str,
) -> List[Dict[str, Any]]:
    """Keep Paddle's structured blocks and add likely missing Tesseract lines.

    A line overlapping a table is recovered only when its recognized tokens are
    absent from Paddle's content and Tesseract confidence is sufficient. Raw
    recognized values remain unchanged; character corrections are suggestions.
    """
    if not tesseract_lines:
        # Preserve the recognized value; corrections are review suggestions only.
        for i, b in enumerate(paddle_blocks):
            _suggest_correction(b)
            b["order"] = i
        return paddle_blocks

    # Collect table bounding boxes from PaddleOCR
    table_bboxes = []
    for b in paddle_blocks:
        if b.get("block_label") == "table" and "bbox" in b and b["bbox"]:
            table_bboxes.append(tuple(b["bbox"]))

    # Normalize existing text in PaddleOCR blocks for duplicate detection
    paddle_text_pool = set()
    for b in paddle_blocks:
        content = str(b.get("content", ""))
        for token in re.findall(r'\w{3,}', content.lower()):
            paddle_text_pool.add(token)

    # A table bbox can be much larger than the text retained in its HTML.
    # Compare recognized tokens before discarding an overlapping line.
    missed_lines: List[Dict[str, Any]] = []

    for line in tesseract_lines:
        line_box = line["bbox"]
        # An overlapping table line needs stronger evidence before recovery.
        inside_table = any(_boxes_overlap(line_box, tbl_box, iou_threshold=0.3) for tbl_box in table_bboxes)
        line_tokens = [tok for tok in re.findall(r'\w{3,}', line["text"].lower())]
        if line_tokens:
            matches = sum(1 for tok in line_tokens if tok in paddle_text_pool)
            match_ratio = matches / len(line_tokens)
            if match_ratio >= 0.7:
                # Already captured in Paddle text blocks
                continue
        if inside_table and (not line_tokens or line.get("confidence", 0) < 0.5):
            continue

        missed_lines.append(line)

    # If missed lines exist, cluster adjacent lines into paragraph blocks
    recovered_blocks: List[Dict[str, Any]] = []
    if missed_lines:
        log.info(
            "Hybrid reconciliation recovered %d text lines missed by layout detection (p%d)",
            len(missed_lines), page_num
        )
        # Sort missed lines by vertical position
        missed_lines.sort(key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))

        current_cluster = [missed_lines[0]]
        for line in missed_lines[1:]:
            prev_line = current_cluster[-1]
            prev_box = prev_line["bbox"]
            curr_box = line["bbox"]
            # If vertical gap is within 2x line height, cluster into same block
            line_height = max(15, prev_box[3] - prev_box[1])
            v_gap = curr_box[1] - prev_box[3]
            if 0 <= v_gap <= (line_height * 2.2):
                current_cluster.append(line)
            else:
                # Flush cluster
                c_text = "\n".join(ln["text"] for ln in current_cluster)
                c_conf = sum(ln["confidence"] for ln in current_cluster) / len(current_cluster)
                c_bbox = (
                    min(ln["bbox"][0] for ln in current_cluster),
                    min(ln["bbox"][1] for ln in current_cluster),
                    max(ln["bbox"][2] for ln in current_cluster),
                    max(ln["bbox"][3] for ln in current_cluster),
                )
                recovered_blocks.append({
                    "block_label": "text",
                    "content": c_text,
                    "confidence": round(c_conf, 4),
                    "bbox": c_bbox,
                    "page": page_num,
                    "source_file": source_file,
                    "format": file_format,
                    "recovery_source": "tesseract",
                })
                current_cluster = [line]

        if current_cluster:
            c_text = "\n".join(ln["text"] for ln in current_cluster)
            c_conf = sum(ln["confidence"] for ln in current_cluster) / len(current_cluster)
            c_bbox = (
                min(ln["bbox"][0] for ln in current_cluster),
                min(ln["bbox"][1] for ln in current_cluster),
                max(ln["bbox"][2] for ln in current_cluster),
                max(ln["bbox"][3] for ln in current_cluster),
            )
            recovered_blocks.append({
                "block_label": "text",
                "content": c_text,
                "confidence": round(c_conf, 4),
                "bbox": c_bbox,
                "page": page_num,
                "source_file": source_file,
                "format": file_format,
                "recovery_source": "tesseract",
            })

    # Combine existing Paddle blocks with recovered blocks
    all_blocks = list(paddle_blocks) + recovered_blocks

    # Preserve raw recognized values and leave unavailable confidence unknown.
    for b in all_blocks:
        _suggest_correction(b)
        confidence = b.get("confidence")
        if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            b["confidence"] = None

    # Sort all blocks by vertical reading order (y1, then x1)
    def block_sort_key(b):
        bbox = b.get("bbox")
        if bbox and len(bbox) >= 2:
            return (bbox[1], bbox[0])
        return (999999, 0)

    all_blocks.sort(key=block_sort_key)

    # Re-assign sequential order indices
    for i, b in enumerate(all_blocks):
        b["order"] = i

    return all_blocks
