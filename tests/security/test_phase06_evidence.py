"""Private VLM review, association, and workload evidence contracts."""

import json
import sys
from datetime import date

import fitz
import pytest

from scripts import measure_representative_gpu as gpu_load
from scripts import prepare_vlm_review as vlm
from scripts.prepare_phase04_associations import (
    association_template,
    verify_associations,
)
from scripts.private_evidence import write_private_json


def test_local_vlm_draft_requires_image_review_before_ground_truth(tmp_path, monkeypatch):
    asset_dir = tmp_path / "test_asset"
    asset_dir.mkdir()
    source = asset_dir / "RFQ_06_scanned.pdf"
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Order 123")
    pdf.save(source)
    pdf.close()
    monkeypatch.setattr(vlm, "ROOT", tmp_path)
    monkeypatch.setattr(vlm, "SCANS", (source.name,))
    monkeypatch.setattr(vlm, "FIELD_KEYS", {source.name: ("rfq_id",)})

    private = tmp_path / "private"
    manifest = vlm.render_corpus(private, dpi=150)
    assert len(manifest["documents"][source.name]["pages"]) == 1
    runner = tmp_path / "local-runner"
    runner.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "assert '--image' in sys.argv and 'Allowed field keys: rfq_id' in sys.stdin.read()\n"
        "print(json.dumps({'transcript': 'Order 123', 'fields': "
        "[{'key':'rfq_id','value':'123','page':1,'label':'Order',"
        "'row':None,'column':None,'evidence':'Order 123'}], 'uncertain': []}))\n",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    vlm.collect_drafts(private / "manifest.json", runner, "local-test-model", [])
    review_path = private / "review_template.json"
    review = vlm.make_review_template(private / "vlm_drafts" / "drafts.json", review_path)
    with pytest.raises(ValueError, match="reviewer"):
        vlm.finalize_review(review_path, private / "ground_truth.json")
    review["verified_by"] = "independent-reviewer"
    review["verified_at"] = date.today().isoformat()
    page = review["documents"][source.name]["pages"][0]
    page["verified_from_image"] = True
    review["documents"][source.name]["fields"]["rfq_id"]["verified_from_image"] = True
    revised_path = private / "reviewed.json"
    write_private_json(revised_path, review)
    result = vlm.finalize_review(revised_path, private / "ground_truth.json")
    assert result["documents"][source.name]["fields"] == {"rfq_id": "123"}
    assert result["documents"][source.name]["field_locations"]["rfq_id"]["label"] == "Order"


def test_itemized_association_recomputes_counts_and_binds_ocr(tmp_path):
    name = "RFQ_06_scanned.pdf"
    location = {"page": 1, "label": "Price", "row": 2, "column": "Value",
                "excerpt": "Price 180"}
    labels = {name: {"transcript": "Price 125\nPrice 180", "fields": {"value_2": "180"},
                     "field_locations": {"value_2": location}}}
    extracted = {"ground_truth_sha256": "a" * 64,
                 "documents": {name: {"content": "Price 125\nPrice 180"}}}
    review = association_template(labels, "a" * 64, extracted, "b" * 64)
    review["verified_by"] = "independent-reviewer"
    review["verified_at"] = date.today().isoformat()
    item = review["items"][0]
    item.update({"observed_value": "180", "observed_location": location,
                 "verdict": "correct", "reviewed_from_image_and_ocr": True})
    checked = verify_associations(review, labels, "a" * 64, extracted, "b" * 64)
    assert checked["correct"] == checked["total"] == 1
    review["correct"] = 5
    with pytest.raises(ValueError, match="total"):
        verify_associations(review, labels, "a" * 64, extracted, "b" * 64)
    del review["correct"]
    item["observed_location"] = {**location, "row": 1}
    with pytest.raises(ValueError, match="Correct association"):
        verify_associations(review, labels, "a" * 64, extracted, "b" * 64)


def test_workload_manifest_expands_bounded_classes_tenants_and_arrivals(tmp_path, monkeypatch):
    assets = tmp_path / "test_asset"
    assets.mkdir()
    for name in ("scan.pdf", "image.jpg", "digital.pdf"):
        (assets / name).write_bytes(name.encode())
    monkeypatch.setattr(gpu_load, "ASSETS", assets.resolve())
    manifest_path = tmp_path / "workload.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "series": [
        {"id": "scan", "fixture": "scan.pdf", "tenant": "a", "category": "scan",
         "count": 2, "start_seconds": 0, "every_seconds": 4},
        {"id": "image", "fixture": "image.jpg", "tenant": "b", "category": "image",
         "count": 1, "start_seconds": 2, "every_seconds": 0},
        {"id": "digital", "fixture": "digital.pdf", "tenant": "a", "category": "digital",
         "count": 1, "start_seconds": 9, "every_seconds": 0},
    ]}), encoding="utf-8")
    cases = gpu_load.expand_manifest(manifest_path)
    assert [case["scheduled_at_seconds"] for case in cases] == [0, 2, 4, 9]
    assert len({case["tenant"] for case in cases}) == 2
    assert all(len(case["source_sha256"]) == 64 for case in cases)
    bad = json.loads(manifest_path.read_text())
    bad["series"][0]["fixture"] = "../outside.pdf"
    manifest_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="inside test_asset"):
        gpu_load.expand_manifest(manifest_path)
