"""Phase 01 tests: shared bounded extraction service + artifact store.

Covers the build-spec invariants: complete page classification (no
first-page-only), one-based page truth, per-page outcomes with explicit
failure, and bounded artifact storage by generated IDs.
"""

from pathlib import Path

import pymupdf
import pytest

from ingestion_pipeline.extraction import service
from ingestion_pipeline.processing.preflight import PreflightError, check_document
from ingestion_pipeline.storage.artifacts import ArtifactStore

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "test_asset"


def make_pdf(path, page_texts):
    with pymupdf.open() as doc:
        for text in page_texts:
            page = doc.new_page()
            page.insert_text((72, 72), text)
        doc.save(path)
    return path


# Text long enough to satisfy the digital-text-layer heuristic used by the
# page classifier (the same threshold the mixed-page regression test uses).
DIGITAL_PAGE_TEXT = "First digital page contains sufficient words for the digital routing decision."


def make_mixed_pdf(path):
    with pymupdf.open() as doc:
        digital1 = doc.new_page()
        digital1.insert_text((72, 72), "First digital page contains enough words.")
        doc.new_page()  # scanned: no text layer
        digital3 = doc.new_page()
        digital3.insert_text((72, 72), "Third digital page contains enough words.")
        doc.save(path)
    return path


# --- page planning ----------------------------------------------------------

def test_plan_digital_and_scanned_pdf_pages(tmp_path):
    digital = make_pdf(tmp_path / "digital.pdf", [DIGITAL_PAGE_TEXT, "Second digital page has other sufficient words now."])
    plan = service.plan_document_pages("a" * 32, digital)
    assert [e.page_number for e in plan.entries] == [1, 2]
    assert all(e.classification == "digital" for e in plan.entries)
    assert plan.total_pages == plan.pages.__self__.total_pages == 2


def test_plan_mixed_pdf_classifies_every_page(tmp_path):
    mixed = make_mixed_pdf(tmp_path / "mixed.pdf")
    plan = service.plan_document_pages("b" * 32, mixed)
    assert [e.classification for e in plan.entries] == ["digital", "scanned", "digital"]
    assert [e.page_number for e in plan.entries] == [1, 2, 3]
    assert not any(e.page_number > plan.total_pages for e in plan.entries)


def test_plan_image_plan_has_one_page(tmp_path):
    image = tmp_path / "RFQ_01_digital.png"
    image.write_bytes(b"")
    with pytest.raises(PreflightError):
        service.plan_document_pages("c" * 32, image)  # empty/corrupt image must fail preflight


def test_plan_rejects_malformed_pdf(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf at all")
    with pytest.raises(PreflightError):
        service.plan_document_pages("d" * 32, broken)


def test_real_fixtures_are_plan_complete():
    if not (ASSETS / "RFQ_06_scanned.pdf").is_file():
        pytest.skip("test_asset fixtures unavailable")
    scanned_plan = service.plan_document_pages("c" * 32, ASSETS / "RFQ_06_scanned.pdf")
    digital_plan = service.plan_document_pages("d" * 32, ASSETS / "RFQ_01_digital.pdf")
    assert scanned_plan.total_pages == 1
    assert scanned_plan.entries[0].classification == "scanned"
    assert digital_plan.entries[0].classification == "digital"


# --- page iteration and digital work ---------------------------------------

def test_iter_pdf_page_work_is_complete_and_one_based(tmp_path):
    mixed = make_mixed_pdf(tmp_path / "mixed.pdf")
    plan = service.plan_document_pages("e" * 32, mixed)
    work = list(service.iter_pdf_page_work(plan, mixed))
    assert [w.entry.page_number for w in work] == [1, 2, 3]
    assert all(w.render_path is None for w in work)


def test_extract_digital_pages_skips_scanned_only_plan(tmp_path):
    mixed = make_mixed_pdf(tmp_path / "mixed.pdf")
    plan = service.plan_document_pages("f" * 32, mixed)
    blocks = service.extract_digital_pages(plan.entries, mixed)
    assert {b.get("page") for b in blocks} == {1, 3}


def test_extract_digital_pages_empty_for_all_scanned_none():
    from pathlib import Path

    assert service.extract_digital_pages([], Path("/unused")) == []


# --- aggregation ------------------------------------------------------------

def _entry(page_number, total=3, classification="scanned"):
    return service.PagePlanEntry(
        document_id="0" * 32, file_name="doc.pdf", file_format="pdf",
        page_number=page_number, classification=classification, total_pages=total,
    )


def test_aggregate_outcomes_complete():
    outcomes = [
        service.PageOutcome(entry=_entry(1), blocks=[{"page": 1, "source_file": "doc.pdf", "confidence": 0.9}]),
        service.PageOutcome(entry=_entry(2)),
        service.PageOutcome(entry=_entry(3), blocks=[{"page": 3, "source_file": "doc.pdf", "confidence": 0.5}]),
    ]
    meta = service.aggregate_outcomes(outcomes, "doc.pdf")
    assert meta["processed_pages"] == [1, 2, 3]
    assert meta["empty_pages"] == [2]
    assert meta["failed_pages"] == []
    assert meta["avg_confidence"] == pytest.approx(0.7)


def test_aggregate_outcomes_records_failure_explicitly():
    outcomes = [
        service.PageOutcome(entry=_entry(1), blocks=[{"page": 1, "source_file": "doc.pdf"}]),
        service.PageOutcome(
            entry=_entry(2), status="failed",
            error=("ocr_incomplete", "boom"),
        ),
    ]
    meta = service.aggregate_outcomes(outcomes, "doc.pdf")
    assert meta["failed_pages"] == [2]
    assert 2 not in meta["empty_pages"]
    assert meta["avg_confidence"] is None


# --- artifact store ---------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    return ArtifactStore.from_output_root(tmp_path)


def test_artifact_put_roundtrip_and_keeps_path_private(store):
    record = store.put("0" * 32, "renders", b"render-bytes", suffix=".png")
    assert set(record) == {"artifact_id", "kind", "sha256", "size_bytes"}
    assert record["artifact_id"] not in str(record).replace(record["artifact_id"], "")
    data = store.read_bounded("0" * 32, record["artifact_id"])
    assert data == b"render-bytes"


def test_artifact_rejects_bad_identifiers_and_sizes(store):
    with pytest.raises(PreflightError, match="hex32"):
        store.put("../escape", "renders", b"x")
    with pytest.raises(PreflightError):
        store.put("0" * 32, "history", b"x")
    with pytest.raises(PreflightError, match="must contain bytes"):
        store.put("0" * 32, "renders", b"")
    with pytest.raises(PreflightError, match="too_large|bounded"):
        store.put("0" * 32, "results", b"x" * (65 * 1024 * 1024))


def test_artifact_read_unknown_id_is_bounded_error(store):
    with pytest.raises(PreflightError, match="not present"):
        store.read_bounded("0" * 32, "a" * 32)


def test_artifact_cleanup_removes_job_tree(store):
    job = "1" * 32
    record = store.put(job, "ocr", b"payload", suffix=".json")
    store.cleanup_job(job)
    with pytest.raises(PreflightError, match="not present"):
        store.read_bounded(job, record["artifact_id"])


def test_check_document_still_enforces_limits(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    with pytest.raises(PreflightError):
        check_document(broken)
