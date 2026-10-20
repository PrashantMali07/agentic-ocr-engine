"""Evaluate manually transcribed scan values without printing document text.

Run with ``PYTHONPATH=src python scripts/evaluate_phase03_scan_fields.py --device cpu``.
This is an opt-in real-model check, not part of the fast unit suite.
"""

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "tests" / "fixtures" / "phase03_scanned_fields.json"


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    args = parser.parse_args()
    os.environ["PADDLE_USE_GPU"] = "true" if args.device == "gpu" else "false"

    import paddle

    from ingestion_pipeline.api import transform_to_document_map
    from ingestion_pipeline.extraction.manager import process_email_attachments

    labels = json.loads(LABELS.read_text(encoding="utf-8"))["documents"]
    paths = [ROOT / "test_asset" / name for name in labels]
    with tempfile.TemporaryDirectory() as temporary:
        record = process_email_attachments(paths, temporary, "scan-field-evaluation")
        documents = transform_to_document_map(record)["documents"]

    actual_device = paddle.get_device()
    if args.device == "cpu" and actual_device != "cpu":
        raise RuntimeError(f"CPU requested but Paddle selected {actual_device}")
    if args.device == "gpu" and not actual_device.startswith("gpu:"):
        raise RuntimeError(f"GPU requested but Paddle selected {actual_device}")

    total = matched = 0
    per_document = {}
    for name, fields in labels.items():
        content = normalized(documents[name]["content"])
        missing = [field for field, value in fields.items() if normalized(value) not in content]
        total += len(fields)
        matched += len(fields) - len(missing)
        per_document[name] = {
            "matched": len(fields) - len(missing),
            "total": len(fields),
            "missing_field_names": missing,
        }
    print(json.dumps({
        "device": actual_device,
        "matched": matched,
        "total": total,
        "presence_rate": round(matched / total, 4),
        "documents": per_document,
    }, indent=2))


if __name__ == "__main__":
    main()
