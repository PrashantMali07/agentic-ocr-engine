"""Model cache integrity checks for the production OCR image."""

import json

import pytest

from ingestion_pipeline.model_artifacts import MODEL_NAMES, collect_artifacts, verify_artifacts


def test_model_manifest_rejects_changed_missing_and_linked_artifacts(tmp_path):
    root = tmp_path / "official_models"
    for name in MODEL_NAMES:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "inference.yml").write_text(name, encoding="utf-8")
        (directory / "config.json").write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": 1, "models": sorted(MODEL_NAMES), "files": collect_artifacts(root),
    }), encoding="utf-8")
    verify_artifacts(root, manifest)

    target = root / sorted(MODEL_NAMES)[0] / "config.json"
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="changed="):
        verify_artifacts(root, manifest)

    target.unlink()
    with pytest.raises(ValueError, match="missing="):
        verify_artifacts(root, manifest)

    target.symlink_to(manifest)
    with pytest.raises(ValueError, match="Linked or special"):
        verify_artifacts(root, manifest)
