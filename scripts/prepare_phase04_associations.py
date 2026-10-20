"""Create and verify itemized, private OCR field/table association evidence.

The reviewer compares each source scan with private OCR text. Machine checks
bind the checklist to exact source and OCR bytes and reject unchecked totals;
they cannot independently prove that a human inspected the images.
"""

import argparse
import json
from datetime import date
from pathlib import Path

if __package__:
    from .evaluate_phase04_acceptance import load_ground_truth, normalized_text, value_present
    from .private_evidence import sha256_file, write_private_json
else:
    from evaluate_phase04_acceptance import load_ground_truth, normalized_text, value_present
    from private_evidence import sha256_file, write_private_json


def _location(value: object, *, excerpt_key: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Each association needs a location")
    page, label, excerpt = value.get("page"), value.get("label"), value.get(excerpt_key)
    if (isinstance(page, bool) or not isinstance(page, int) or page < 1
            or not isinstance(label, str) or not label.strip()
            or not isinstance(excerpt, str) or not excerpt.strip()):
        raise ValueError("Association location needs page, label, and source excerpt")
    row, column = value.get("row"), value.get("column")
    if (row is None) != (column is None):
        raise ValueError("Table association requires both row and column")
    for coordinate in (row, column):
        if coordinate is not None and (isinstance(coordinate, bool) or
            not isinstance(coordinate, (int, str)) or
            isinstance(coordinate, int) and coordinate < 1 or
            isinstance(coordinate, str) and not coordinate.strip()):
            raise ValueError("Table row and column must be positive or named")
    return {"page": page, "label": label, "row": row, "column": column,
            excerpt_key: excerpt}


def reference_locations(labels: dict) -> dict[tuple[str, str], dict]:
    locations = {}
    for name, entry in labels.items():
        fields, named = entry["fields"], entry.get("field_locations")
        if not isinstance(named, dict) or set(named) != set(fields):
            raise ValueError("Reviewed labels need a location for every field")
        for key in fields:
            location = _location(named[key], excerpt_key="excerpt")
            if normalized_text(location["excerpt"]) not in normalized_text(entry["transcript"]):
                raise ValueError("Reference excerpt is absent from the reviewed transcript")
            if not value_present(fields[key], location["excerpt"]):
                raise ValueError("Reference excerpt does not contain the reviewed field value")
            locations[(name, key)] = location
    return locations


def load_extracted(path: Path, labels: dict, label_digest: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or payload.get("ground_truth_sha256") != label_digest
            or not isinstance(payload.get("documents"), dict)
            or set(payload["documents"]) != set(labels)):
        raise ValueError("Private OCR output is not bound to the reviewed corpus")
    for entry in payload["documents"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
            raise ValueError("Private OCR output needs document text")
    return payload


def association_template(labels: dict, label_digest: str, extracted: dict,
                         extracted_digest: str) -> dict:
    references = reference_locations(labels)
    items = []
    for (name, key), reference in sorted(references.items()):
        items.append({
            "document": name, "field": key, "expected_value": labels[name]["fields"][key],
            "reference_location": reference, "observed_value": "",
            "observed_location": {"page": None, "label": "", "row": None,
                                  "column": None, "excerpt": ""},
            "verdict": None, "reason": "", "reviewed_from_image_and_ocr": False,
        })
    return {"schema_version": 1, "verified_by": "", "verified_at": "",
            "ground_truth_sha256": label_digest, "ocr_output_sha256": extracted_digest,
            "items": items}


def verify_associations(review: dict, labels: dict, label_digest: str,
                        extracted: dict, extracted_digest: str) -> dict:
    if not isinstance(review, dict) or review.get("schema_version") != 1:
        raise ValueError("Association evidence needs schema version 1")
    reviewer = review.get("verified_by")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Association evidence needs a reviewer")
    try:
        reviewed = date.fromisoformat(review["verified_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Association evidence needs an ISO verification date") from exc
    if reviewed > date.today():
        raise ValueError("Association verification date is in the future")
    if (review.get("ground_truth_sha256") != label_digest
            or review.get("ocr_output_sha256") != extracted_digest):
        raise ValueError("Association evidence is not tied to reviewed source and OCR bytes")
    if (extracted.get("ground_truth_sha256") != label_digest
            or set(extracted.get("documents", {})) != set(labels)):
        raise ValueError("Private OCR output is not bound to the reviewed corpus")
    references = reference_locations(labels)
    items = review.get("items")
    if not isinstance(items, list) or len(items) != len(references):
        raise ValueError("Association checklist is incomplete")
    seen = set()
    correct = 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Association item must be an object")
        identity = (item.get("document"), item.get("field"))
        if identity not in references or identity in seen:
            raise ValueError("Association checklist has unknown or repeated fields")
        seen.add(identity)
        name, key = identity
        if (item.get("expected_value") != labels[name]["fields"][key]
                or item.get("reference_location") != references[identity]
                or item.get("reviewed_from_image_and_ocr") is not True):
            raise ValueError("Association item changed the reference or was not reviewed")
        verdict = item.get("verdict")
        if verdict not in ("correct", "incorrect"):
            raise ValueError("Every association needs an explicit verdict")
        observed = item.get("observed_location")
        observed_value = item.get("observed_value")
        if not isinstance(observed_value, str) or not isinstance(observed, dict):
            raise ValueError("Association observation is invalid")
        actual = normalized_text(extracted["documents"][name]["content"])
        if observed.get("excerpt"):
            observed = _location(observed, excerpt_key="excerpt")
            if normalized_text(observed["excerpt"]) not in actual:
                raise ValueError("Observed excerpt is absent from the exact OCR output")
        elif verdict == "correct":
            raise ValueError("A correct association needs OCR context evidence")
        reference = references[identity]
        same = (normalized_text(observed_value) == normalized_text(item["expected_value"])
                and observed.get("page") == reference["page"]
                and normalized_text(str(observed.get("label", ""))) == normalized_text(reference["label"])
                and observed.get("row") == reference["row"]
                and observed.get("column") == reference["column"])
        if verdict == "correct":
            if (not same or not observed_value.strip() or
                    not value_present(observed_value, observed["excerpt"])):
                raise ValueError("Correct association does not match the verified value and location")
            correct += 1
        elif not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("Incorrect association needs a reviewer reason")
    if seen != set(references):
        raise ValueError("Association checklist is incomplete")
    if "correct" in review and review["correct"] != correct:
        raise ValueError("Association correct total does not match itemized verdicts")
    if "total" in review and review["total"] != len(items):
        raise ValueError("Association total does not match itemized verdicts")
    return {**review, "correct": correct, "total": len(items)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("template", "finalize"))
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--review", type=Path, help="Completed itemized template, required for finalize")
    args = parser.parse_args()
    try:
        labels = load_ground_truth(args.ground_truth)
        label_digest = sha256_file(args.ground_truth)
        extracted = load_extracted(args.extracted, labels, label_digest)
        extracted_digest = sha256_file(args.extracted)
        if args.command == "template":
            payload = association_template(labels, label_digest, extracted, extracted_digest)
        else:
            if args.review is None:
                parser.error("finalize requires --review")
            payload = verify_associations(json.loads(args.review.read_text(encoding="utf-8")),
                                          labels, label_digest, extracted, extracted_digest)
        write_private_json(args.output, payload)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Association review failed ({type(exc).__name__}: {exc})")
    print(args.output)


if __name__ == "__main__":
    main()
