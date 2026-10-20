"""Release quality gate rejects sparse, unreviewed, or unbound evidence."""

from collections import Counter
from datetime import date

import pytest

from ingestion_pipeline.job_store import _bounded_capacity
from ingestion_pipeline.services.release_metrics import nearest_rank
from scripts.check_phase06_quality import check_quality


def _evidence():
    today = date.today().isoformat()
    digest, extracted_digest, jobs_digest, workload_digest = (letter * 64 for letter in "abcd")
    labels = {}
    extracted = {"schema_version": 1, "ground_truth_sha256": digest, "documents": {}}
    association_items = []
    for index in range(6, 11):
        name = f"RFQ_{index:02d}_scanned.pdf"
        excerpt = f"Order {index}"
        location = {"page": 1, "label": "Order", "row": None, "column": None,
                    "excerpt": excerpt}
        labels[name] = {"transcript": excerpt, "fields": {"rfq_id": str(index)},
                        "field_locations": {"rfq_id": location}}
        extracted["documents"][name] = {"content": excerpt}
        association_items.append({"document": name, "field": "rfq_id", "expected_value": str(index),
            "reference_location": location, "observed_value": str(index),
            "observed_location": dict(location), "verdict": "correct", "reason": "",
            "reviewed_from_image_and_ocr": True})
    policy = {
        "verified_by": "business-owner", "verified_at": today,
        "max_cer": 0.05, "max_wer": 0.1,
        "min_field_value_presence": 0.98, "min_association_accuracy": 0.99,
        "max_failure_rate": 0.01, "max_retry_rate": 0.01,
        "max_p95_seconds": 90, "max_p99_seconds": 120,
        "min_throughput_jobs_per_minute": 1,
        "max_peak_rss_bytes": 8_000_000_000,
        "max_peak_gpu_bytes": 8_000_000_000,
        "max_peak_artifact_bytes": 10_000_000,
        "min_gpu_duration_seconds": 3600, "min_gpu_jobs": 100,
    }
    accuracy = {"device": "gpu:0", "ground_truth_sha256": digest,
        "documents": dict.fromkeys(labels, {}), "cer": 0.02, "wer": 0.05,
        "field_value_presence": 1.0}
    cases = []
    jobs = []
    for index in range(100):
        category = "scanned_pdf" if index % 2 else "digital_pdf"
        source_digest = str(index % 3) * 64
        common = {"case_id": f"case-{index:03d}", "fixture": f"fixture-{index % 3}.pdf",
            "source_sha256": source_digest, "size_bytes": (index % 3 + 1) * 1024,
            "format": "pdf", "tenant": f"tenant-{index % 2}", "category": category,
            "scheduled_at_seconds": index + index // 2, "expect": "accepted"}
        cases.append(common)
        jobs.append({**common, "admission_http_status": 202, "job_id": f"job-{index:03d}",
                     "status": "completed", "nonempty_result": True,
                     "submitted_at_seconds": common["scheduled_at_seconds"],
                     "admitted_at_seconds": common["scheduled_at_seconds"] + 1,
                     "finished_at_seconds": common["scheduled_at_seconds"] + 61,
                     "end_to_end_seconds": 60})
    scheduled = sorted(job["scheduled_at_seconds"] for job in jobs)
    intervals = sorted({scheduled[index] - scheduled[index - 1]
                        for index in range(1, len(scheduled))})
    load = {"route": "POST /extract/v1 + GET /extract/v1/{job_id}",
        "gpu": "Test GPU", "profile": "representative", "ready_at_end": True,
        "fixture_count": 3, "nonempty_document_results": True,
        "job_count": 100, "scheduled_count": 100, "rejected_count": 0,
        "statuses": {"completed": 100}, "alerts_at_end": [],
        "attempt_counts": {"completed": 100},
        "wall_seconds": 4000, "p95_end_to_end_seconds": 60,
        "p99_end_to_end_seconds": 60,
        "throughput_jobs_per_minute": 1.5,
        "peak_rss_bytes": 5_000_000_000, "peak_gpu_bytes": 3_000_000_000,
        "peak_artifact_bytes": 1_000_000, "failure_rate": 0,
        "retry_attempts": 0, "per_job_evidence_sha256": jobs_digest,
        "workload_manifest_sha256": workload_digest,
        "document_classes": dict(Counter(job["category"] for job in jobs)),
        "tenant_count": 2, "distinct_input_sizes": 3,
        "arrival_span_seconds": scheduled[-1] - scheduled[0],
        "arrival_intervals_seconds": intervals, "peak_queued": 3,
        "peak_processing": 1}
    associations = {"schema_version": 1, "verified_by": "independent-reviewer",
        "verified_at": today, "ground_truth_sha256": digest,
        "ocr_output_sha256": extracted_digest, "items": association_items,
        "total": len(association_items), "correct": len(association_items)}
    return (policy, labels, digest, accuracy, load, associations, extracted,
            extracted_digest, {"schema_version": 1, "jobs": jobs}, jobs_digest, cases,
            workload_digest)


def test_quality_gate_requires_itemized_association_and_representative_jobs():
    evidence = _evidence()
    assert check_quality(*evidence)["passed"]
    load, associations, jobs = evidence[4], evidence[5], evidence[8]

    load["job_count"] = 3
    load["statuses"] = {"completed": 3}
    with pytest.raises(ValueError, match="incomplete"):
        check_quality(*evidence)
    load["job_count"] = 100
    load["statuses"] = {"completed": 100}
    associations["items"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        check_quality(*evidence)
    associations["items"] = _evidence()[5]["items"]
    associations["correct"] = 4
    with pytest.raises(ValueError, match="total"):
        check_quality(*evidence)
    associations["correct"] = 5
    associations["items"][0]["observed_location"]["label"] = "Wrong"
    with pytest.raises(ValueError, match="Correct association"):
        check_quality(*evidence)
    associations["items"] = _evidence()[5]["items"]
    jobs["jobs"][0]["tenant"] = "other-tenant"
    with pytest.raises(ValueError, match="hashed workload"):
        check_quality(*evidence)
    jobs["jobs"][0]["tenant"] = "tenant-0"
    load["document_classes"] = {"scanned_pdf": 100}
    with pytest.raises(ValueError, match="Aggregate GPU"):
        check_quality(*evidence)


def test_percentiles_and_capacity_bounds(monkeypatch):
    assert nearest_rank(list(range(1, 101)), 95) == 95
    assert nearest_rank(list(range(1, 101)), 99) == 99
    assert nearest_rank([], 99) is None
    monkeypatch.setenv("INGESTION_MAX_PENDING_JOBS", "0")
    with pytest.raises(ValueError, match="between"):
        _bounded_capacity("INGESTION_MAX_PENDING_JOBS", 8, 64)
