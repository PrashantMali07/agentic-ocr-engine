"""Fast, reproducible Phase 3 checks against the local document fixture set."""

import re
from collections import Counter
from pathlib import Path

import pymupdf
import pytest
from openpyxl import Workbook

from ingestion_pipeline.api import transform_to_document_map
from ingestion_pipeline.extraction.manager import process_email_attachments
from ingestion_pipeline.processing.preflight import SUPPORTED, check_document

ASSETS = Path(__file__).resolve().parents[2] / "test_asset"


@pytest.fixture(scope="module", autouse=True)
def require_assets():
    if not ASSETS.is_dir():
        pytest.skip("Local test_asset fixture directory is unavailable")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]{4,}", text)}


def test_all_supported_asset_headers_pass_preflight():
    files = [path for path in ASSETS.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED]
    counts = Counter(check_document(path).format for path in files)
    assert counts["pdf"] >= 10
    assert counts["png"] >= 5
    assert counts["jpg"] >= 5
    assert counts["docx"] >= 1
    assert counts["html"] >= 1


def test_digital_pdf_fixtures_keep_text_and_page_accounting(tmp_path):
    paths = [ASSETS / f"RFQ_{number:02d}_digital.pdf" for number in range(1, 6)]
    record = process_email_attachments(paths, str(tmp_path / "out"), "digital-assets")
    documents = transform_to_document_map(record)["documents"]
    for path in paths:
        with pymupdf.open(path) as source:
            reference = _tokens(source[0].get_text())
        result = documents[path.name]
        extracted = _tokens(result["content"])
        assert result["status"] == "complete"
        assert result["processed_pages"] == [1]
        assert not result["empty_pages"]
        assert len(reference & extracted) / len(reference) >= 0.95


def test_native_asset_and_generated_tabular_formats(tmp_path):
    csv = tmp_path / "sample.csv"
    csv.write_text("Reference,Value\nABC-123,42\n", encoding="utf-8")
    xlsx = tmp_path / "sample.xlsx"
    workbook = Workbook()
    workbook.active.append(["Reference", "Value"])
    workbook.active.append(["ABC-123", 42])
    workbook.save(xlsx)
    workbook.close()
    docx = ASSETS / "test_pipeline_01/attachments/Shipment-Details-Germany-To-India.docx"
    html = ASSETS / "test_pipeline_01/attachments/0335EXP010292927_20260620060245.html"
    inputs = [csv, xlsx, docx, html]
    record = process_email_attachments(inputs, str(tmp_path / "out"), "native-assets")
    documents = transform_to_document_map(record)["documents"]
    for path in inputs:
        assert documents[path.name]["status"] == "complete"
        assert documents[path.name]["content"].strip()
    assert "ABC-123" in documents[csv.name]["content"]
    assert "ABC-123" in documents[xlsx.name]["content"]
