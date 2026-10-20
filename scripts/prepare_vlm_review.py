"""Render scanned PDFs, collect local VLM drafts, and prepare human-reviewed labels.

The local runner receives ``--image PATH`` and a prompt on stdin, and must
print one JSON object to stdout. It must never receive our OCR output. This
script does not choose/download a model or send documents to a cloud service.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path

if __package__:
    from .prepare_phase04_review import FIELD_KEYS, ROOT, SCANS
    from .private_evidence import create_private_dir, sha256_file, write_private_bytes, write_private_json
else:
    from prepare_phase04_review import FIELD_KEYS, ROOT, SCANS
    from private_evidence import create_private_dir, sha256_file, write_private_bytes, write_private_json


PROMPT_VERSION = "rfq-visible-text-v1"
MAX_PAGES = 40
MAX_PIXELS = 30_000_000
MAX_IMAGE_BYTES = 60 * 1024 * 1024
MAX_CORPUS_BYTES = 2 * 1024 * 1024 * 1024
MAX_RESPONSE_BYTES = 2_000_000
PROMPT = (
    "Transcribe this document page from the image only. Return JSON with "
    "transcript (full visible text in reading order), fields (list of objects "
    "with key, value, page, label, row, column, evidence), and uncertain "
    "(list of unreadable spans). Preserve numbers, table rows and empty cells. "
    "Do not infer missing text. Allowed field keys: {keys}. Page: {page}."
)


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Evidence must be a JSON object")
    return value


def render_corpus(output_dir: Path, *, dpi: int = 200) -> dict:
    if not 100 <= dpi <= 300:
        raise ValueError("DPI must be between 100 and 300")
    import fitz

    create_private_dir(output_dir)
    documents = {}
    rendered_bytes = 0
    for name in SCANS:
        source = ROOT / "test_asset" / name
        with fitz.open(source) as pdf:
            if not 1 <= len(pdf) <= MAX_PAGES:
                raise ValueError("PDF page count is outside the bounded review range")
            pages = []
            for index, page in enumerate(pdf, 1):
                matrix = fitz.Matrix(dpi / 72, dpi / 72)
                bounds = page.rect * matrix
                if bounds.width * bounds.height > MAX_PIXELS:
                    raise ValueError("Rendered page exceeds the pixel budget")
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image_name = f"{Path(name).stem}-page-{index:04d}.png"
                image_bytes = pixmap.tobytes("png")
                rendered_bytes += len(image_bytes)
                if len(image_bytes) > MAX_IMAGE_BYTES or rendered_bytes > MAX_CORPUS_BYTES:
                    raise ValueError("Rendered images exceed the private review storage budget")
                image_sha = write_private_bytes(output_dir / image_name, image_bytes)
                pages.append({"page": index, "image": image_name, "image_sha256": image_sha,
                              "width": pixmap.width, "height": pixmap.height})
        documents[name] = {"sha256": sha256_file(source), "pages": pages}
    manifest = {"schema_version": 1, "dpi": dpi, "color_mode": "RGB", "documents": documents}
    write_private_json(output_dir / "manifest.json", manifest)
    return manifest


def _checked_manifest(path: Path) -> dict:
    manifest = _load_json(path)
    if manifest.get("schema_version") != 1 or set(manifest.get("documents", {})) != set(SCANS):
        raise ValueError("Manifest does not cover the expected scanned corpus")
    for name, entry in manifest["documents"].items():
        if sha256_file(ROOT / "test_asset" / name) != entry.get("sha256"):
            raise ValueError("Source PDF changed after rendering")
        pages = entry.get("pages")
        if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
            raise ValueError("Manifest has invalid page coverage")
        for index, page in enumerate(pages, 1):
            image = page.get("image")
            image_path = path.parent / image if isinstance(image, str) else path
            if (page.get("page") != index or not isinstance(image, str)
                    or not re.fullmatch(r"RFQ_\d{2}_scanned-page-\d{4}\.png", image)
                    or image_path.is_symlink()
                    or sha256_file(image_path) != page.get("image_sha256")):
                raise ValueError("Rendered page digest or order changed")
    return manifest


def _parse_draft(value: object, name: str, page: int) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("transcript"), str):
        raise ValueError("VLM output needs a transcript")
    fields, uncertain = value.get("fields"), value.get("uncertain")
    if not isinstance(fields, list) or not isinstance(uncertain, list) or any(
        not isinstance(item, str) for item in uncertain
    ):
        raise ValueError("VLM output needs fields and uncertain lists")
    seen = set()
    for item in fields:
        if not isinstance(item, dict) or item.get("key") not in FIELD_KEYS[name] or item["key"] in seen:
            raise ValueError("VLM field key is unknown or repeated on a page")
        seen.add(item["key"])
        if (item.get("page") != page or not all(isinstance(item.get(key), str)
                for key in ("value", "label", "evidence"))
                or item.get("row") is not None and not isinstance(item["row"], (int, str))
                or item.get("column") is not None and not isinstance(item["column"], (int, str))):
            raise ValueError("VLM field location has an invalid shape")
    return {"transcript": value["transcript"], "fields": fields, "uncertain": uncertain}


def collect_drafts(manifest_path: Path, runner: Path, model_id: str,
                   runner_args: list[str], *, timeout: int = 180) -> dict:
    manifest = _checked_manifest(manifest_path)
    if not model_id.strip() or not runner.is_file() or not os.access(runner, os.X_OK):
        raise ValueError("A named, executable local VLM runner is required")
    if not 1 <= timeout <= 1800:
        raise ValueError("Runner timeout must be 1–1800 seconds")
    drafts = {}
    for name, entry in manifest["documents"].items():
        pages = []
        for page in entry["pages"]:
            prompt = PROMPT.format(keys=", ".join(FIELD_KEYS[name]), page=page["page"])
            response = subprocess.run(
                [str(runner), *runner_args, "--image", str(manifest_path.parent / page["image"])],
                input=prompt, text=True, capture_output=True, timeout=timeout, check=False,
            )
            if response.returncode != 0 or len(response.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ValueError("Local VLM runner failed or exceeded the response budget")
            try:
                parsed = _parse_draft(json.loads(response.stdout), name, page["page"])
            except json.JSONDecodeError as exc:
                raise ValueError("Local VLM runner did not return JSON") from exc
            pages.append({"page": page["page"], "image_sha256": page["image_sha256"],
                          "raw_response": response.stdout, **parsed})
        drafts[name] = {"sha256": entry["sha256"], "pages": pages}
    payload = {"schema_version": 1, "model_id": model_id, "prompt_version": PROMPT_VERSION,
               "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
               "manifest_sha256": sha256_file(manifest_path), "documents": drafts}
    destination = manifest_path.parent / "vlm_drafts"
    create_private_dir(destination)
    write_private_json(destination / "drafts.json", payload)
    return payload


def make_review_template(drafts_path: Path, output: Path) -> dict:
    drafts = _load_json(drafts_path)
    manifest_path = drafts_path.parent.parent / "manifest.json"
    if output.parent.resolve() != manifest_path.parent.resolve():
        raise ValueError("Keep the review template beside its rendered-page manifest")
    manifest = _checked_manifest(manifest_path)
    if drafts.get("manifest_sha256") != sha256_file(manifest_path):
        raise ValueError("VLM drafts do not match the page manifest")
    documents = {}
    for name in SCANS:
        entry = drafts.get("documents", {}).get(name)
        if not isinstance(entry, dict) or entry.get("sha256") != manifest["documents"][name]["sha256"]:
            raise ValueError("VLM drafts do not cover the source PDFs")
        draft_pages = entry.get("pages")
        source_pages = manifest["documents"][name]["pages"]
        if not isinstance(draft_pages, list) or len(draft_pages) != len(source_pages):
            raise ValueError("VLM drafts have incomplete page coverage")
        fields = {key: {"value": "", "page": None, "label": "", "row": None,
                        "column": None, "evidence": "", "verified_from_image": False}
                  for key in FIELD_KEYS[name]}
        pages = []
        for source_page, draft_page in zip(source_pages, draft_pages, strict=False):
            if (draft_page.get("page") != source_page["page"]
                    or draft_page.get("image_sha256") != source_page["image_sha256"]):
                raise ValueError("VLM draft page order or image digest changed")
            checked = _parse_draft(draft_page, name, source_page["page"])
            pages.append({"page": source_page["page"], "image_sha256": source_page["image_sha256"],
                          "transcript": checked["transcript"], "uncertain": checked["uncertain"],
                          "verified_from_image": False, "reviewer_note": ""})
            for item in checked["fields"]:
                fields[item["key"]].update({"value": item["value"], "page": item["page"],
                    "label": item["label"], "row": item.get("row"),
                    "column": item.get("column"), "evidence": item["evidence"]})
        documents[name] = {"sha256": entry["sha256"], "pages": pages, "fields": fields}
    template = {"schema_version": 1, "verified_by": "", "verified_at": "",
                "vlm_draft_sha256": sha256_file(drafts_path), "documents": documents}
    write_private_json(output, template)
    return template


def finalize_review(review_path: Path, output: Path) -> dict:
    review = _load_json(review_path)
    draft_path = review_path.parent / "vlm_drafts" / "drafts.json"
    if (review.get("schema_version") != 1
            or review.get("vlm_draft_sha256") != sha256_file(draft_path)):
        raise ValueError("Reviewed labels are not bound to the VLM draft file")
    reviewer = review.get("verified_by")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Independent reviewer identity is required")
    try:
        reviewed = date.fromisoformat(review["verified_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Review needs an ISO verification date") from exc
    if reviewed > date.today():
        raise ValueError("Verification date cannot be in the future")
    if set(review.get("documents", {})) != set(SCANS):
        raise ValueError("Review must cover every scanned PDF")
    labels = {}
    for name, entry in review["documents"].items():
        if sha256_file(ROOT / "test_asset" / name) != entry.get("sha256"):
            raise ValueError("Reviewed source PDF digest changed")
        pages = entry.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError("Review has no pages")
        rendered_manifest = review_path.parent / "manifest.json"
        manifest = _checked_manifest(rendered_manifest)
        source_pages = manifest["documents"][name]["pages"]
        if len(pages) != len(source_pages):
            raise ValueError("Review has incomplete page coverage")
        transcript_parts = []
        for expected, page in zip(source_pages, pages, strict=False):
            if (page.get("page") != expected["page"] or page.get("image_sha256") != expected["image_sha256"]
                    or page.get("verified_from_image") is not True
                    or not isinstance(page.get("transcript"), str) or not page["transcript"].strip()):
                raise ValueError("Every page needs an image-verified transcript")
            transcript_parts.append(page["transcript"].strip())
        transcript = "\n\n".join(transcript_parts)
        fields = entry.get("fields")
        if not isinstance(fields, dict) or set(fields) != set(FIELD_KEYS[name]):
            raise ValueError("Review has missing or extra field keys")
        values, locations = {}, {}
        for key, field in fields.items():
            if (not isinstance(field, dict) or field.get("verified_from_image") is not True
                    or not isinstance(field.get("value"), str) or not field["value"].strip()
                    or not isinstance(field.get("page"), int) or not 1 <= field["page"] <= len(pages)
                    or not isinstance(field.get("label"), str) or not field["label"].strip()
                    or not isinstance(field.get("evidence"), str) or not field["evidence"].strip()
                    or field["evidence"].casefold() not in transcript.casefold()):
                raise ValueError("Every field needs a value, location, and transcript evidence")
            values[key] = field["value"]
            locations[key] = {"page": field["page"], "label": field["label"],
                              "row": field.get("row"), "column": field.get("column"),
                              "excerpt": field["evidence"]}
        labels[name] = {"sha256": entry["sha256"], "transcript": transcript,
                        "fields": values, "field_locations": locations}
    result = {"verified_by": reviewer, "verified_at": review["verified_at"],
              "review_file_sha256": sha256_file(review_path), "documents": labels}
    write_private_json(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--output-dir", required=True, type=Path)
    render.add_argument("--dpi", type=int, default=200)
    draft = sub.add_parser("draft")
    draft.add_argument("--manifest", required=True, type=Path)
    draft.add_argument("--runner", required=True, type=Path)
    draft.add_argument("--runner-arg", action="append", default=[])
    draft.add_argument("--model-id", required=True)
    draft.add_argument("--timeout", type=int, default=180)
    review = sub.add_parser("review-template")
    review.add_argument("--drafts", required=True, type=Path)
    review.add_argument("--output", required=True, type=Path)
    final = sub.add_parser("finalize")
    final.add_argument("--review", required=True, type=Path)
    final.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "render":
            render_corpus(args.output_dir, dpi=args.dpi)
            destination = args.output_dir / "manifest.json"
        elif args.command == "draft":
            collect_drafts(args.manifest, args.runner, args.model_id, args.runner_arg, timeout=args.timeout)
            destination = args.manifest.parent / "vlm_drafts" / "drafts.json"
        elif args.command == "review-template":
            make_review_template(args.drafts, args.output)
            destination = args.output
        else:
            finalize_review(args.review, args.output)
            destination = args.output
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        parser.error(f"Review preparation failed ({type(exc).__name__}: {exc})")
    print(destination)


if __name__ == "__main__":
    main()
