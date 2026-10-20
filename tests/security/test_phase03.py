import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
import pymupdf
import pytest
from openpyxl import Workbook
from PIL import Image

from ingestion_pipeline import api
from ingestion_pipeline.extraction import csv_parser, excel, manager, ocr
from ingestion_pipeline.extraction.hybrid import reconcile_paddle_and_tesseract
from ingestion_pipeline.job_store import JobStore
from ingestion_pipeline.processing.preflight import PreflightError, check_document
from ingestion_pipeline.schema import JobStatus
from ingestion_pipeline.security.auth import Principal
from ingestion_pipeline.services import job_runner


def make_pdf(path: Path, texts: list[str]):
    document = pymupdf.open()
    for text in texts:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def test_pdf_page_limit_encryption_and_corruption(tmp_path):
    many = tmp_path / "many.pdf"
    make_pdf(many, ["page"] * 51)
    with pytest.raises(PreflightError, match="page count") as limit:
        check_document(many)
    assert limit.value.code == "page_limit"

    protected = tmp_path / "protected.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "private text")
    document.save(protected, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                  user_pw="secret", owner_pw="owner")
    document.close()
    with pytest.raises(PreflightError) as password:
        check_document(protected)
    assert password.value.code == "password_required"

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nnot a valid PDF")
    with pytest.raises(PreflightError) as corrupt:
        check_document(broken)
    assert corrupt.value.code == "corrupt_document"


def test_image_pixels_and_frames_are_bounded(tmp_path):
    huge = tmp_path / "huge.png"
    Image.new("1", (5000, 5000)).save(huge)
    with pytest.raises(PreflightError) as dimensions:
        check_document(huge)
    assert dimensions.value.code == "image_dimensions"

    frames = tmp_path / "frames.tiff"
    Image.new("RGB", (10, 10)).save(frames, save_all=True,
                                    append_images=[Image.new("RGB", (10, 10))])
    with pytest.raises(PreflightError) as multi:
        check_document(frames)
    assert multi.value.code == "image_frames"


def test_archive_expansion_and_unsafe_entry_rejected(tmp_path):
    bomb = tmp_path / "bomb.docx"
    with ZipFile(bomb, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * (2 * 1024 * 1024))
    with pytest.raises(PreflightError) as ratio:
        check_document(bomb)
    assert ratio.value.code == "archive_ratio"

    unsafe = tmp_path / "unsafe.docx"
    with ZipFile(unsafe, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr("../outside", "x")
    with pytest.raises(PreflightError) as path:
        check_document(unsafe)
    assert path.value.code == "archive_path"

    encrypted_wrapper = tmp_path / "protected.docx"
    encrypted_wrapper.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 64)
    with pytest.raises(PreflightError) as corrupt:
        check_document(encrypted_wrapper)
    assert corrupt.value.code == "corrupt_document"

    late_nul = tmp_path / "late.csv"
    late_nul.write_bytes(b"a" * 70_000 + b"\x00")
    with pytest.raises(PreflightError) as binary:
        check_document(late_nul)
    assert binary.value.code == "corrupt_document"


def test_spreadsheet_and_csv_limits_and_escaping(tmp_path, monkeypatch):
    book = tmp_path / "book.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["<tag>", "O100"])
    sheet.append(["last", "row"])
    workbook.save(book)
    workbook.close()
    assert check_document(book).format == "xlsx"
    record = excel.parse_excel(str(book))
    assert "&lt;tag&gt;" in record["blocks"][0]["content"]
    assert "O100" in record["blocks"][0]["content"]
    monkeypatch.setattr(excel, "MAX_ROWS_PER_SHEET", 1)
    with pytest.raises(PreflightError) as sheet_limit:
        excel.parse_excel(str(book))
    assert sheet_limit.value.code == "sheet_limit"

    csv = tmp_path / "rows.csv"
    csv.write_text("a,b\n1,2\n3,4\n")
    monkeypatch.setattr(csv_parser, "MAX_ROWS", 2)
    with pytest.raises(PreflightError) as csv_limit:
        csv_parser.parse_csv(str(csv))
    assert csv_limit.value.code == "csv_limit"


@pytest.mark.parametrize("scanned_has_text", [True, False])
def test_mixed_pdf_page_order_and_partial_status(tmp_path, monkeypatch, scanned_has_text):
    pdf = tmp_path / "mixed.pdf"
    make_pdf(pdf, [
        "First digital page contains sufficient text for digital routing.",
        "",
        "Third digital page contains sufficient text for digital routing.",
    ])
    rendered = []
    monkeypatch.setattr(manager, "get_ocr_pipeline", lambda: object())
    monkeypatch.setattr(manager, "clear_gpu_cache", lambda: None)

    def fake_render(path, page_num, out_dir):
        rendered.append(page_num)
        return str(Path(out_dir) / "page.png")

    monkeypatch.setattr(manager, "render_pdf_page_to_image", fake_render)
    monkeypatch.setattr(manager, "prepare_image_for_ocr", lambda path: np.zeros((10, 10, 3), dtype=np.uint8))
    monkeypatch.setattr(manager, "ocr_predict", lambda pipeline, image: [object()])
    monkeypatch.setattr(manager, "extract_tesseract_lines", lambda image: [])
    monkeypatch.setattr(manager, "pp_structure_to_blocks", lambda result, source, page: ([{
        "block_label": "text", "content": "Second scanned page text", "order": 0,
        "page": page, "confidence": None, "source_file": source, "format": "pdf",
    }] if scanned_has_text else []))

    record = manager.process_email_attachments([pdf], str(tmp_path / "out"), "job")
    assert rendered == [2]
    result = api.transform_to_document_map(record)["documents"]["mixed.pdf"]
    assert result["pages"] == 3
    assert result["processed_pages"] == [1, 2, 3]
    assert result["empty_pages"] == ([] if scanned_has_text else [2])
    assert result["status"] == ("complete" if scanned_has_text else "partial")
    if scanned_has_text:
        content = result["content"]
        assert content.index("First digital") < content.index("Second scanned") < content.index("Third digital")


def test_ocr_correction_is_only_a_suggestion():
    blocks = [{"block_label": "text", "content": "ORDER 1O0", "order": 0,
               "page": 1, "confidence": None, "source_file": "scan.pdf"}]
    corrected = reconcile_paddle_and_tesseract(blocks, [], "scan.pdf", 1, "pdf")
    assert corrected[0]["content"] == "ORDER 1O0"
    assert corrected[0]["suggested_content"] == "ORDER 100"
    record = {"email_id": "job", "attachments": [{"file": "scan.pdf", "format": "pdf", "pages": 1}],
              "blocks": corrected}
    doc = api.transform_to_document_map(record)["documents"]["scan.pdf"]
    assert doc["content"] == "ORDER 1O0"
    assert doc["avg_confidence"] is None
    assert doc["correction_suggestions"][0]["proposed"] == "ORDER 100"


@pytest.mark.parametrize("use_gpu, expected", [(False, "cpu"), (True, "gpu:0")])
def test_ocr_pipeline_selects_configured_device(monkeypatch, use_gpu, expected):
    selected = []
    monkeypatch.setattr(ocr, "PADDLE_USE_GPU", use_gpu)
    monkeypatch.setattr(ocr.paddle, "set_device", selected.append)
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PPStructureV3=lambda **kwargs: kwargs))
    ocr.get_ocr_pipeline.cache_clear()
    try:
        pipeline = ocr.get_ocr_pipeline()
        assert selected == [expected]
        assert pipeline["device"] == expected
    finally:
        ocr.get_ocr_pipeline.cache_clear()


def test_tesseract_recovers_text_missing_from_overlapping_table():
    table = [{"block_label": "table", "content": "<table><td>Known value</td></table>",
              "bbox": (0, 0, 200, 200), "order": 0, "page": 1,
              "confidence": 0.95, "source_file": "scan.pdf"}]
    lines = [
        {"text": "Known value", "bbox": (10, 10, 110, 30), "confidence": 0.9},
        {"text": "Missing reference ABC123", "bbox": (10, 50, 180, 70), "confidence": 0.9},
        {"text": "Uncertain reference XYZ999", "bbox": (10, 90, 180, 110), "confidence": 0.3},
    ]
    merged = reconcile_paddle_and_tesseract(table, lines, "scan.pdf", 1, "pdf")
    assert len(merged) == 2
    assert merged[0]["block_label"] == "table"
    assert merged[1]["content"] == "Missing reference ABC123"
    assert merged[1]["recovery_source"] == "tesseract"


def test_invalid_document_fails_once_with_safe_code(tmp_path, monkeypatch):
    output = tmp_path / "output"
    job_id = "corrupt-job"
    document_id = "a" * 32
    inputs = output / "jobs" / job_id / "attachments"
    inputs.mkdir(parents=True)
    (inputs / f"{document_id}.pdf").write_bytes(b"%PDF-1.7\nbroken")
    store = JobStore(output / "jobs.db")
    monkeypatch.setattr(job_runner, "job_store", store)
    monkeypatch.setattr(job_runner, "DEFAULT_OUTPUT_DIR", str(output))
    owner = Principal("tenant", "owner", frozenset({"extract"}))

    async def prepare():
        await store.admit(job_id, owner, [{"document_id": document_id,
            "original_name": "broken.pdf", "size": 15}], 15)
        assert await store.claim_next("worker")
    asyncio.run(prepare())

    assert job_runner.run(job_id, "worker") == 0
    job = asyncio.run(store.get(job_id))
    assert job.status == JobStatus.failed
    assert job.error.startswith("corrupt_document:")


def test_partial_document_commits_partial_job(tmp_path, monkeypatch):
    output = tmp_path / "output"
    job_id = "partial-job"
    document_id = "b" * 32
    inputs = output / "jobs" / job_id / "attachments"
    inputs.mkdir(parents=True)
    (inputs / f"{document_id}.pdf").write_bytes(b"%PDF-1.7")
    store = JobStore(output / "jobs.db")
    monkeypatch.setattr(job_runner, "job_store", store)
    monkeypatch.setattr(job_runner, "DEFAULT_OUTPUT_DIR", str(output))
    monkeypatch.setattr(job_runner, "process_email_attachments", lambda **kwargs: {
        "email_id": job_id,
        "attachments": [{"file": f"{document_id}.pdf", "format": "pdf", "pages": 2,
                         "processed_pages": [1, 2], "empty_pages": [2]}],
        "blocks": [{"source_file": f"{document_id}.pdf", "order": 0, "page": 1,
                    "content": "page one", "confidence": 1.0}],
    })
    owner = Principal("tenant", "owner", frozenset({"extract"}))

    async def prepare():
        await store.admit(job_id, owner, [{"document_id": document_id,
            "original_name": "partial.pdf", "size": 8}], 8)
        assert await store.claim_next("worker")
    asyncio.run(prepare())

    assert job_runner.run(job_id, "worker") == 0
    job = asyncio.run(store.get(job_id))
    assert job.status == JobStatus.partial
    assert job.result.documents["partial.pdf"].empty_pages == [2]
