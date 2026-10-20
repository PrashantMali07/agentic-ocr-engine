"""Prefetch the active PP-StructureV3 model set into PaddleX cache at build time.

Keep these model-selecting options aligned with extraction/ocr.py's default
legacy pipeline. The disposable build environment uses CPU deliberately.
"""
from paddleocr import PPStructureV3

PPStructureV3(
    device="cpu",
    layout_nms=True,
    layout_unclip_ratio=1.1,
    layout_merge_bboxes_mode="union",
    text_det_thresh=0.2,
    text_det_box_thresh=0.4,
    text_det_unclip_ratio=1.8,
    text_det_limit_side_len=1600,
    text_det_limit_type="max",
    text_rec_score_thresh=0.5,
    use_doc_orientation_classify=True,
    use_doc_unwarping=False,
    use_table_recognition=True,
)
