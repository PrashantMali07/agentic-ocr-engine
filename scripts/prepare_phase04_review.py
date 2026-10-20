"""Create a blank, private review template for the five scanned RFQ fixtures.

The Phase 3 field names are used as a checklist, but no Phase 3 values or OCR
output are copied into the template. An independent reviewer must transcribe
the full visible document and verify each expected value against the PDF.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANS = tuple(f"RFQ_{number:02d}_scanned.pdf" for number in range(6, 11))
FIELD_KEYS = {
    "RFQ_06_scanned.pdf": ("rfq_id", "date", "customer", "hsn_1", "quantity_1", "value_1", "hsn_2", "value_2"),
    "RFQ_07_scanned.pdf": ("rfq_id", "date", "customer", "hsn_1", "quantity_1", "value_1", "hsn_2", "value_2"),
    "RFQ_08_scanned.pdf": ("rfq_id", "date", "customer", "hsn_1", "quantity_1", "value_1", "hsn_2", "value_2"),
    "RFQ_09_scanned.pdf": ("rfq_id", "date", "customer", "hsn_1", "quantity_1", "value_1", "hsn_2", "value_2", "hsn_3", "value_3", "hsn_4", "value_4"),
    "RFQ_10_scanned.pdf": ("rfq_id", "date", "customer", "hsn_1", "quantity_1", "value_1", "hsn_2", "value_2", "hsn_3", "value_3"),
}


def template() -> dict:
    return {
        "verified_by": "",
        "verified_at": "",
        "documents": {
            name: {
                "sha256": hashlib.sha256((ROOT / "test_asset" / name).read_bytes()).hexdigest(),
                "transcript": "",
                "fields": {key: "" for key in FIELD_KEYS[name]},
                "field_locations": {key: {"page": None, "label": "", "row": None,
                                         "column": None, "excerpt": ""}
                                    for key in FIELD_KEYS[name]},
            }
            for name in SCANS
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "phase04_review")
    args = parser.parse_args()
    output_dir = args.output_dir
    try:
        output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        parser.error("Review directory already exists; refusing to overwrite reviewer work")
    except OSError as exc:
        parser.error(f"Cannot create review directory ({type(exc).__name__})")
    os.chmod(output_dir, 0o700)
    destination = output_dir / "ground_truth_template.json"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(template(), stream, indent=2)
        stream.write("\n")
    print(destination)


if __name__ == "__main__":
    main()
