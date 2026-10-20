"""Score real scans against independently verified private transcripts.

Input JSON:
  {"verified_by":"reviewer/team", "verified_at":"YYYY-MM-DD",
   "documents":{"RFQ_06_scanned.pdf": {
      "sha256":"64 lowercase hex digits", "transcript":"full page text",
      "fields":{"rfq_id":"RFQ/2026/1006"}}}}

Only metrics and missing field names are printed. Do not commit transcripts
containing private document data; the supplied Phase 3 value fixture is not
accepted as verified ground truth.
"""

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

if __package__:
    from .private_evidence import write_private_json
else:
    from private_evidence import write_private_json


ROOT = Path(__file__).resolve().parent.parent


def normalized_text(value: str) -> str:
    # PP-StructureV3 stores recognized tables as sanitized HTML inside the
    # document content. Score their visible cell text, not HTML tag names.
    value = re.sub(
        r"<table\b[^>]*>.*?</table\s*>",
        lambda match: BeautifulSoup(match.group(), "html.parser").get_text(" ", strip=True),
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", html.unescape(value)).casefold()).strip()


def value_present(expected: str, actual: str) -> bool:
    """Allow formatting differences without matching a value inside a larger token."""
    tokens = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", expected).casefold())
    if not tokens:
        return False
    pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])"
    return re.search(pattern, unicodedata.normalize("NFKC", actual).casefold()) is not None


def edit_distance(left: str | list[str], right: str | list[str]) -> int:
    if len(right) > len(left):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row_index, left_item in enumerate(left, 1):
        current = [row_index]
        for column_index, right_item in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (left_item != right_item),
            ))
        previous = current
    return previous[-1]


def load_ground_truth(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or not isinstance(payload.get("verified_by"), str)
            or not payload["verified_by"].strip() or not isinstance(payload.get("verified_at"), str)):
        raise ValueError("Ground truth needs independent reviewer identity and verification date")
    try:
        date.fromisoformat(payload["verified_at"])
    except ValueError as exc:
        raise ValueError("Ground truth verification date must be ISO format") from exc
    documents = payload.get("documents")
    if not isinstance(documents, dict) or not documents:
        raise ValueError("Ground truth needs at least one document")
    for name, entry in documents.items():
        if (not isinstance(name, str) or not re.fullmatch(r"RFQ_\d{2}_scanned\.pdf", name)
                or not (ROOT / "test_asset" / name).is_file()):
            raise ValueError("Ground truth references an unknown scanned fixture")
        if (not isinstance(entry, dict) or not isinstance(entry.get("transcript"), str)
                or not entry["transcript"].strip() or not isinstance(entry.get("fields"), dict)
                or not entry["fields"] or any(
                    not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                    for key, value in entry["fields"].items()
                )):
            raise ValueError("Each document needs a full transcript and nonempty field values")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Each document needs a lowercase SHA-256 source digest")
        actual_digest = hashlib.sha256((ROOT / "test_asset" / name).read_bytes()).hexdigest()
        if digest != actual_digest:
            raise ValueError("Ground truth source digest does not match the scanned fixture")
    return documents


def score_documents(labels: dict, extracted: dict) -> dict:
    total_char_errors = total_chars = total_word_errors = total_words = 0
    total_fields = matched_fields = 0
    per_document = {}
    for name, reference in labels.items():
        actual = normalized_text(extracted[name])
        expected = normalized_text(reference["transcript"])
        chars = len(expected)
        words = expected.split()
        char_errors = edit_distance(expected, actual)
        word_errors = edit_distance(words, actual.split())
        missing = [key for key, value in reference["fields"].items()
                   if not value_present(value, actual)]
        total_char_errors += char_errors
        total_chars += chars
        total_word_errors += word_errors
        total_words += len(words)
        total_fields += len(reference["fields"])
        matched_fields += len(reference["fields"]) - len(missing)
        per_document[name] = {
            "cer": round(char_errors / chars, 4),
            "wer": round(word_errors / len(words), 4),
            "field_value_presence": round(1 - len(missing) / len(reference["fields"]), 4),
            "missing_field_names": missing,
        }
    return {
        "cer": round(total_char_errors / total_chars, 4),
        "wer": round(total_word_errors / total_words, 4),
        "field_value_presence": round(matched_fields / total_fields, 4),
        "documents": per_document,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "gpu"), required=True)
    parser.add_argument("--max-cer", type=float)
    parser.add_argument("--max-wer", type=float)
    parser.add_argument("--min-field-value-presence", type=float)
    parser.add_argument("--private-extracted-output", type=Path,
                        help="Write private OCR text for itemized association review; refuses overwrite")
    args = parser.parse_args()
    thresholds = (args.max_cer, args.max_wer, args.min_field_value_presence)
    if any(value is not None and not 0 <= value <= 1 for value in thresholds):
        parser.error("Accuracy thresholds must be between 0 and 1")
    try:
        labels = load_ground_truth(args.ground_truth)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    os.environ["PADDLE_USE_GPU"] = "true" if args.device == "gpu" else "false"

    import paddle

    from ingestion_pipeline.api import transform_to_document_map
    from ingestion_pipeline.extraction.manager import process_email_attachments

    paths = [ROOT / "test_asset" / name for name in labels]
    with tempfile.TemporaryDirectory(prefix="ingest-acceptance-") as output:
        record = process_email_attachments(paths, output, "verified-scan-evaluation")
        extracted = {name: item["content"] for name, item in
                     transform_to_document_map(record)["documents"].items()}
    device = paddle.get_device()
    if (args.device == "cpu" and device != "cpu") or (args.device == "gpu" and not device.startswith("gpu:")):
        raise RuntimeError("OCR ran on a different device than requested")
    label_digest = hashlib.sha256(args.ground_truth.read_bytes()).hexdigest()
    if args.private_extracted_output:
        write_private_json(args.private_extracted_output, {
            "schema_version": 1, "ground_truth_sha256": label_digest,
            "documents": {name: {"content": extracted[name]} for name in labels},
        })
    scores = score_documents(labels, extracted)
    failed = ((args.max_cer is not None and scores["cer"] > args.max_cer)
              or (args.max_wer is not None and scores["wer"] > args.max_wer)
              or (args.min_field_value_presence is not None
                  and scores["field_value_presence"] < args.min_field_value_presence))
    print(json.dumps({"device": device, "ground_truth_file": args.ground_truth.name,
                      "ground_truth_sha256": label_digest,
                      "thresholds_applied": any(value is not None for value in thresholds),
                      "passed_thresholds": None if all(value is None for value in thresholds) else not failed,
                      **scores}, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
