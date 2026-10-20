"""Fail-closed quality/capacity gate for independently reviewed OCR evidence.

This checks accuracy and GPU load evidence only. Image scans, backups,
monitoring, and ingress still require their separate operator release gates.
No OCR or image build is run by this script.
"""

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path

if __package__:
    from .evaluate_phase04_acceptance import load_ground_truth
    from .measure_representative_gpu import expand_manifest
    from .prepare_phase04_associations import verify_associations
    from .private_evidence import sha256_file
else:
    from evaluate_phase04_acceptance import load_ground_truth
    from measure_representative_gpu import expand_manifest
    from prepare_phase04_associations import verify_associations
    from private_evidence import sha256_file

from ingestion_pipeline.services.release_metrics import nearest_rank

SUCCESS = {"completed", "partial", "waiting_for_review"}
RATIO_KEYS = ("max_cer", "max_wer", "min_field_value_presence", "min_association_accuracy",
              "max_failure_rate", "max_retry_rate")
POSITIVE_KEYS = ("max_p95_seconds", "max_p99_seconds", "min_throughput_jobs_per_minute",
                 "max_peak_rss_bytes", "max_peak_gpu_bytes", "max_peak_artifact_bytes",
                 "min_gpu_duration_seconds")


def _number(value, name: str, *, minimum: float = 0, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < minimum or (strict and value == minimum):
        raise ValueError(f"{name} is below its allowed minimum")
    return float(value)


def _reviewed(payload: dict, name: str) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("verified_by"), str) or not payload["verified_by"].strip():
        raise ValueError(f"{name} needs a reviewer")
    try:
        reviewed = date.fromisoformat(payload["verified_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} needs an ISO verification date") from exc
    if reviewed > date.today():
        raise ValueError(f"{name} verification date is in the future")


def _check_job_evidence(load: dict, evidence: dict, evidence_digest: str,
                        workload_cases: list[dict], workload_digest: str) -> None:
    jobs = evidence.get("jobs") if isinstance(evidence, dict) else None
    if (load.get("profile") != "representative" or load.get("per_job_evidence_sha256") != evidence_digest
            or load.get("workload_manifest_sha256") != workload_digest
            or not isinstance(jobs, list) or len(jobs) != load.get("scheduled_count")
            or load.get("rejected_count") != 0):
        raise ValueError("Representative per-job GPU evidence is missing or inconsistent")
    if (len({job.get("case_id") for job in jobs if isinstance(job, dict)}) != len(jobs)
            or any(not isinstance(job, dict) or job.get("admission_http_status") != 202
                   or job.get("expect") != "accepted" or job.get("nonempty_result") is not True
                   or not isinstance(job.get("job_id"), str) or not job["job_id"]
                   or not isinstance(job.get("source_sha256"), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", job["source_sha256"])
                   for job in jobs)
            or len({job["job_id"] for job in jobs}) != len(jobs)):
        raise ValueError("Per-job GPU evidence has missing or duplicate admitted jobs")
    planned = {case["case_id"]: case for case in workload_cases}
    if len(planned) != len(jobs) or any(
        job["case_id"] not in planned or any(
            job.get(key) != planned[job["case_id"]][key]
            for key in ("fixture", "source_sha256", "size_bytes", "format", "tenant",
                        "category", "scheduled_at_seconds", "expect")
        ) for job in jobs
    ):
        raise ValueError("Per-job evidence does not match the hashed workload manifest")
    values = [job.get("end_to_end_seconds") for job in jobs]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("Per-job GPU evidence needs positive end-to-end timings")
    for job in jobs:
        submitted = job.get("submitted_at_seconds")
        admitted = job.get("admitted_at_seconds")
        finished = job.get("finished_at_seconds")
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) for value in (submitted, admitted, finished))
                or not 0 <= job["scheduled_at_seconds"] <= submitted + 0.02
                or not submitted <= admitted + 0.02 or not admitted < finished
                or abs((finished - admitted) - job["end_to_end_seconds"]) > 0.01):
            raise ValueError("Per-job GPU timestamps do not support reported latency")
    wall = load.get("wall_seconds")
    if (isinstance(wall, bool) or not isinstance(wall, (int, float))
            or wall + 0.02 < max(job["finished_at_seconds"] for job in jobs)):
        raise ValueError("GPU wall time is shorter than itemized job completion")
    if (dict(sorted(Counter(job["status"] for job in jobs).items())) != load.get("statuses")
            or dict(sorted(Counter(job["category"] for job in jobs).items())) != load.get("document_classes")
            or len({job["tenant"] for job in jobs}) != load.get("tenant_count")
            or len({job["size_bytes"] for job in jobs}) != load.get("distinct_input_sizes")
            or len({job["source_sha256"] for job in jobs}) != load.get("fixture_count")
            or nearest_rank(values, 95) != load.get("p95_end_to_end_seconds")
            or nearest_rank(values, 99) != load.get("p99_end_to_end_seconds")):
        raise ValueError("Aggregate GPU metrics do not match itemized jobs")
    times = [job.get("scheduled_at_seconds") for job in jobs]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in times):
        raise ValueError("GPU arrival profile needs valid scheduled times")
    times.sort()
    reported_span = load.get("arrival_span_seconds")
    if (isinstance(reported_span, bool) or not isinstance(reported_span, (int, float))
            or round(times[-1] - times[0], 3) != round(reported_span, 3)
            or sorted({round(times[index] - times[index - 1], 3)
                       for index in range(1, len(times))}) != load.get("arrival_intervals_seconds")):
        raise ValueError("GPU arrival profile does not match itemized jobs")
    if (len(load["document_classes"]) < 2 or load["tenant_count"] < 2
            or load["distinct_input_sizes"] < 3 or len(load["arrival_intervals_seconds"]) < 2
            or load["arrival_span_seconds"] <= 0):
        raise ValueError("GPU workload does not vary classes, sizes, tenants, and arrival rates")
    processing = load.get("peak_processing")
    queued = load.get("peak_queued")
    if (isinstance(processing, bool) or not isinstance(processing, int) or not 0 <= processing <= 1
            or isinstance(queued, bool) or not isinstance(queued, int) or queued < 0):
        raise ValueError("GPU queue concurrency evidence is missing or exceeds one OCR worker")


def check_quality(policy: dict, labels: dict, label_digest: str, accuracy: dict,
                  load: dict, association: dict, extracted: dict, extracted_digest: str,
                  load_jobs: dict, load_jobs_digest: str,
                  workload_cases: list[dict], workload_digest: str) -> dict:
    _reviewed(policy, "Policy")
    _reviewed(association, "Association evidence")
    for key in RATIO_KEYS:
        if _number(policy.get(key), key) > 1:
            raise ValueError(f"{key} must be between 0 and 1")
    for key in POSITIVE_KEYS:
        _number(policy.get(key), key, strict=True)
    min_jobs = policy.get("min_gpu_jobs")
    if isinstance(min_jobs, bool) or not isinstance(min_jobs, int) or min_jobs < 100:
        raise ValueError("min_gpu_jobs must be at least 100 for p99 evidence")

    device = accuracy.get("device")
    if not isinstance(device, str) or not device.startswith("gpu:"):
        raise ValueError("Accuracy evidence must use GPU OCR")
    if accuracy.get("ground_truth_sha256") != label_digest or set(accuracy.get("documents", {})) != set(labels):
        raise ValueError("Accuracy evidence is not tied to the reviewed scanned corpus")
    if len(labels) < 5:
        raise ValueError("Accuracy corpus must include at least five independently reviewed scans")
    for metric, limit, comparison in (
        ("cer", "max_cer", "max"), ("wer", "max_wer", "max"),
        ("field_value_presence", "min_field_value_presence", "min"),
    ):
        value = _number(accuracy.get(metric), metric)
        if value > 1 or (comparison == "max" and value > policy[limit]) or (
            comparison == "min" and value < policy[limit]
        ):
            raise ValueError(f"Accuracy metric {metric} misses the approved threshold")

    checked = verify_associations(association, labels, label_digest, extracted, extracted_digest)
    total, correct = checked["total"], checked["correct"]
    if correct / total < policy["min_association_accuracy"]:
        raise ValueError("Field/table association accuracy misses the approved threshold")

    statuses = load.get("statuses")
    job_count = load.get("job_count")
    if (load.get("route") != "POST /extract/v1 + GET /extract/v1/{job_id}"
            or not isinstance(load.get("gpu"), str) or not load["gpu"].strip()
            or load.get("nonempty_document_results") is not True
            or isinstance(load.get("fixture_count"), bool)
            or not isinstance(load.get("fixture_count"), int)
            or load["fixture_count"] < 3
            or load.get("ready_at_end") is not True
            or not isinstance(statuses, dict) or not statuses
            or not set(statuses).issubset(SUCCESS)
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in statuses.values())
            or isinstance(job_count, bool) or not isinstance(job_count, int)
            or job_count < min_jobs or sum(statuses.values()) != job_count
            or load.get("alerts_at_end") != []):
        raise ValueError("GPU FastAPI load evidence is incomplete or unhealthy")
    _check_job_evidence(load, load_jobs, load_jobs_digest, workload_cases, workload_digest)
    attempts = load.get("attempt_counts")
    if (not isinstance(attempts, dict)
            or any(isinstance(count, bool) or not isinstance(count, int) or count < 0
                   for count in attempts.values())
            or sum(attempts.values()) < job_count):
        raise ValueError("GPU worker attempt evidence is incomplete")
    if _number(load.get("wall_seconds"), "wall_seconds") < policy["min_gpu_duration_seconds"]:
        raise ValueError("GPU load duration is below the approved minimum")
    for metric, limit, comparison in (
        ("p95_end_to_end_seconds", "max_p95_seconds", "max"),
        ("p99_end_to_end_seconds", "max_p99_seconds", "max"),
        ("throughput_jobs_per_minute", "min_throughput_jobs_per_minute", "min"),
        ("peak_rss_bytes", "max_peak_rss_bytes", "max"),
        ("peak_gpu_bytes", "max_peak_gpu_bytes", "max"),
        ("peak_artifact_bytes", "max_peak_artifact_bytes", "max"),
        ("failure_rate", "max_failure_rate", "max"),
    ):
        value = _number(load.get(metric), metric, strict=metric.startswith("peak_") or metric == "throughput_jobs_per_minute")
        if (comparison == "max" and value > policy[limit]) or (
            comparison == "min" and value < policy[limit]
        ):
            raise ValueError(f"GPU load metric {metric} misses the approved threshold")
    retries = load.get("retry_attempts")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0 or retries / job_count > policy["max_retry_rate"]:
        raise ValueError("GPU retry rate misses the approved threshold")
    return {"passed": True, "reviewed_scans": len(labels), "gpu_jobs": job_count,
            "association_accuracy": round(correct / total, 4)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("policy", "ground-truth", "accuracy", "gpu-load", "associations",
                 "extracted", "gpu-jobs", "workload-manifest"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        files = {key: json.loads(getattr(args, key).read_text(encoding="utf-8")) for key in
                 ("policy", "accuracy", "gpu_load", "associations", "extracted", "gpu_jobs")}
        labels = load_ground_truth(args.ground_truth)
        digest = hashlib.sha256(args.ground_truth.read_bytes()).hexdigest()
        result = check_quality(files["policy"], labels, digest, files["accuracy"],
                               files["gpu_load"], files["associations"], files["extracted"],
                               sha256_file(args.extracted), files["gpu_jobs"],
                               sha256_file(args.gpu_jobs), expand_manifest(args.workload_manifest),
                               sha256_file(args.workload_manifest))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
