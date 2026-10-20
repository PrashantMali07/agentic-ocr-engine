"""Verify the exact PP-StructureV3 artifacts bundled into production images.

The manifest is a reviewed build input. It is never regenerated automatically
during an image build: a changed upstream model must fail the build first.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

# These are the model names observed with the active PP-StructureV3 settings.
# Explicit directories stop PaddleX from selecting or downloading another model.
MODEL_DIR_ARGUMENTS = {
    "doc_orientation_classify_model_dir": "PP-LCNet_x1_0_doc_ori",
    "region_detection_model_dir": "PP-DocBlockLayout",
    "layout_detection_model_dir": "PP-DocLayout_plus-L",
    "textline_orientation_model_dir": "PP-LCNet_x1_0_textline_ori",
    "text_detection_model_dir": "PP-OCRv5_server_det",
    "text_recognition_model_dir": "PP-OCRv5_server_rec",
    "table_classification_model_dir": "PP-LCNet_x1_0_table_cls",
    "wired_table_structure_recognition_model_dir": "SLANeXt_wired",
    "wireless_table_structure_recognition_model_dir": "SLANet_plus",
    "wired_table_cells_detection_model_dir": "RT-DETR-L_wired_table_cell_det",
    "wireless_table_cells_detection_model_dir": "RT-DETR-L_wireless_table_cell_det",
    "formula_recognition_model_dir": "PP-FormulaNet_plus-L",
}
MODEL_NAMES = frozenset(MODEL_DIR_ARGUMENTS.values())


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_artifacts(root: Path) -> dict[str, str]:
    """Hash regular model files, rejecting links and incomplete model trees."""
    result = {}
    for name in sorted(MODEL_NAMES):
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"Missing or linked OCR model directory: {name}")
        files = [p for p in directory.rglob("*") if ".cache" not in p.relative_to(directory).parts]
        if not files:
            raise ValueError(f"Empty OCR model directory: {name}")
        if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in files):
            raise ValueError(f"Linked or special OCR model artifact: {name}")
        for path in files:
            if path.is_file():
                result[path.relative_to(root).as_posix()] = _file_hash(path)
        if not any(key.startswith(name + "/inference.") for key in result):
            raise ValueError(f"OCR model has no inference files: {name}")
    return result


def verify_artifacts(root: Path, manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or set(manifest.get("models", [])) != MODEL_NAMES:
        raise ValueError("OCR model manifest has an unexpected model set or schema")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("OCR model manifest has no file hashes")
    actual = collect_artifacts(root)
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(k for k in actual.keys() & expected.keys() if actual[k] != expected[k])
        raise ValueError(f"OCR model artifact mismatch: missing={missing}, extra={extra}, changed={changed}")


def configured_model_directories() -> dict[str, str]:
    """Return verified local-only model paths when image policy is enabled."""
    manifest = os.getenv("OCR_MODEL_MANIFEST")
    if not manifest:
        return {}
    root = Path(os.environ["OCR_MODEL_ROOT"])
    verify_artifacts(root, Path(manifest))
    return {argument: str(root / name) for argument, name in MODEL_DIR_ARGUMENTS.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "record":
        payload = {"schema": 1, "models": sorted(MODEL_NAMES), "files": collect_artifacts(args.root)}
        args.manifest.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    else:
        verify_artifacts(args.root, args.manifest)
    print(f"OCR model artifacts {args.action} complete")


if __name__ == "__main__":
    main()
