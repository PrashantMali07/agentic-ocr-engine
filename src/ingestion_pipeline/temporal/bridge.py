"""Test-lane bridge: admission outbox → workflow, workflow result → job store.

One direction is submission, the other is delivery:

- **Submission (reconciler)**: reads pending ``lane_submissions`` rows,
  resolves the deterministic workflow ID, and starts (or joins) the
  workflow. A crash at any point is recoverable: rows without a workflow
  ID are retried; the ID derivation makes duplicate starts harmless
  (Temporal joins the same workflow).
- **Delivery**: for a terminal or review-paused workflow, the small status
  payload names page artifacts by ID; this module reads them back under
  the tenant/job scope, rebuilds the legacy-shaped ``ExtractResponse``,
  and delivers it through the guarded, idempotent
  ``JobStore.deliver_workflow_result``. Legacy jobs are never touched.

Raw blocks never travel through workflow history here — delivery reads
them from artifacts, exactly like the legacy worker reads staged files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .. import job_store as job_store_module
from ..config import DEFAULT_OUTPUT_DIR
from ..schema import ExtractResponse, ReviewRequest
from ..storage.artifacts import ArtifactStore

log = logging.getLogger("temporal.bridge")

LANE_SCHEDULER = "temporal_test"

STORE = ArtifactStore.from_output_root(Path(DEFAULT_OUTPUT_DIR))

# Injectable in tests; production uses the temporalio client.
_job_store: job_store_module.JobStore = job_store_module.job_store
_workflow_submitter: Optional[Callable[[Dict[str, Any]], Awaitable[str]]] = None
_workflow_status_querier: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None


def set_job_store(store: job_store_module.JobStore) -> None:
    """Test-only seam: run the bridge against an isolated store instance."""
    global _job_store
    _job_store = store


def set_workflow_starter(starter: Callable[[Dict[str, Any]], Any]) -> None:
    global _workflow_submitter
    _workflow_submitter = starter


def set_workflow_status_querier(querier: Callable[[str], Any]) -> None:
    global _workflow_status_querier
    _workflow_status_querier = querier


async def _start_workflow(job_request: Dict[str, Any]) -> str:
    if _workflow_submitter is not None:
        return await _workflow_submitter(job_request)
    from .client import start_ingestion_workflow

    return await start_ingestion_workflow(job_request)


async def _query_workflow_status(job_id: str) -> Dict[str, Any]:
    if _workflow_status_querier is not None:
        return await _workflow_status_querier(job_id)
    from .client import get_workflow_status

    return await get_workflow_status(f"doc-extract-{job_id}")


# ---------------------------------------------------------------------------
# Submission reconciler
# ---------------------------------------------------------------------------

async def reconcile_pending_submissions(limit: int = 25) -> int:
    """Start workflows for pending outbox rows; count successful starts."""
    count = 0
    pending_rows = await _job_store.pending_lane_submissions(
        LANE_SCHEDULER, limit=limit,
    )
    for pending in pending_rows:
        try:
            workflow_id = await _start_workflow(pending["request"])
            await _job_store.record_lane_submission(pending["job_id"], workflow_id)
            await _job_store.set_processing_by_scheduler(pending["job_id"], LANE_SCHEDULER)
            count += 1
        except Exception as exc:
            log.warning(
                "Could not start workflow for job %s (%s: %s); row stays pending",
                pending["job_id"], type(exc).__name__, exc,
            )
    return count


async def mark_processing(job_id: str) -> bool:
    """Flip the admitted queued job to processing (guard: scheduler kind)."""
    return await _job_store.set_processing_by_scheduler(job_id, LANE_SCHEDULER)


# ---------------------------------------------------------------------------
# Result delivery
# ---------------------------------------------------------------------------

def _attachments_meta(page_outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-document metadata in the legacy manager's shape."""
    meta: Dict[str, Dict[str, Any]] = {}
    for outcome in page_outcomes:
        name = outcome["display_name"]
        entry = meta.setdefault(name, {
            "file": name,
            "format": outcome["extension"],
            "pages": None,
            "processed_pages": [],
            "empty_pages": [],
            "failed_pages": [],
        })
        entry["pages"] = (
            outcome["total_pages"] if entry["pages"] is None
            else max(entry["pages"], outcome["total_pages"])
        )
        entry["processed_pages"].append(outcome["page_number"])
        if outcome["status"] == "empty":
            entry["empty_pages"].append(outcome["page_number"])
        elif outcome["status"] == "failed":
            entry["failed_pages"].append(outcome["page_number"])
    for entry in meta.values():
        entry["processed_pages"].sort()
        entry["empty_pages"].sort()
        entry["failed_pages"].sort()
    return list(meta.values())


def _validate_page_accounting(final_payload: Dict[str, Any]) -> None:
    """Reject missing pages, duplicate outcomes, and detached block references."""
    outcomes = final_payload.get("page_outcomes") or []
    refs = final_payload.get("artifact_refs") or []
    if not outcomes:
        if final_payload.get("status") != "failed" or refs:
            raise ValueError("A nonfailed workflow has no page outcomes")
        return
    if len(outcomes) > 10_000 or len(refs) > 10_000:
        raise ValueError("Workflow page summary exceeds contract limit")
    pages_by_document: Dict[str, set[int]] = {}
    totals: Dict[str, int] = {}
    expected_refs: Dict[Tuple[str, int], Tuple[str, str]] = {}
    statuses: list[str] = []
    needs_review = False
    for outcome in outcomes:
        document_id = outcome["document_id"]
        page_number = outcome["page_number"]
        total_pages = outcome["total_pages"]
        status = outcome["status"]
        if (type(page_number) is not int or type(total_pages) is not int
                or page_number < 1 or total_pages < 1 or total_pages > 10_000 or page_number > total_pages
                or status not in {"completed", "empty", "failed", "needs_review"}):
            raise ValueError("Invalid page outcome identity or status")
        pages = pages_by_document.setdefault(document_id, set())
        if page_number in pages or (document_id in totals and totals[document_id] != total_pages):
            raise ValueError("Duplicate page or inconsistent declared total")
        pages.add(page_number)
        totals[document_id] = total_pages
        statuses.append(status)
        needs_review |= status == "needs_review" or bool((outcome.get("audit") or {}).get("requires_hitl"))
        artifact_id = outcome.get("text_artifact")
        sha256 = outcome.get("text_artifact_sha256")
        if status in {"completed", "needs_review"}:
            if not artifact_id or not sha256:
                raise ValueError("Extracted page has no checksummed artifact")
            expected_refs[(document_id, page_number)] = (artifact_id, sha256)
        elif artifact_id or sha256:
            raise ValueError("Empty or failed page cannot claim extracted blocks")
    for document_id, pages in pages_by_document.items():
        if pages != set(range(1, totals[document_id] + 1)):
            raise ValueError("Every declared page needs a terminal outcome")
    actual_refs: Dict[Tuple[str, int], Tuple[str, str]] = {}
    for ref in refs:
        key = (ref["document_id"], ref["page_number"])
        if key in actual_refs:
            raise ValueError("Duplicate block artifact reference")
        actual_refs[key] = (ref["artifact_id"], ref.get("sha256"))
    if actual_refs != expected_refs:
        raise ValueError("Block artifact references do not match page outcomes")
    expected_status = ("failed" if "failed" in statuses else
                       "needs_review" if needs_review else
                       "partial" if "empty" in statuses else "completed")
    reported = final_payload.get("status")
    if reported == "waiting_for_human_review":
        reported = "needs_review"
    if reported != expected_status:
        raise ValueError("Workflow status does not match page outcomes")


async def build_result_payload(page_outcomes: List[Dict[str, Any]], job_id: str,
                               artifact_refs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rebuild the legacy ExtractResponse body from per-page block artifacts."""
    blocks_by_key: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for ref in artifact_refs:
        raw = STORE.read_bounded(job_id, ref["artifact_id"])
        if ref.get("sha256") and hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("Stored block artifact does not match its recorded checksum")
        blocks_by_key[(ref["document_id"], ref["page_number"])] = json.loads(raw.decode("utf-8"))

    combined_blocks: List[Dict[str, Any]] = []
    for key in sorted(blocks_by_key.keys(), key=lambda k: (k[0], k[1])):
        combined_blocks.extend(blocks_by_key[key])

    meta = _attachments_meta(page_outcomes)
    for entry in meta:
        att_outcomes = [o for o in page_outcomes if o["display_name"] == entry["file"]]
        scores = [o["confidence"] for o in att_outcomes if isinstance(o.get("confidence"), (int, float))]
        entry["avg_confidence"] = round(sum(scores) / len(scores), 4) if scores else None

    record = {
        "email_id": job_id,
        "attachments": meta,
        "blocks": combined_blocks,
    }
    from ..api import transform_to_document_map

    payload = transform_to_document_map(record)
    payload["request_id"] = job_id
    for name, document in payload["documents"].items():
        outcomes = [outcome for outcome in page_outcomes if outcome["display_name"] == name]
        document["audit"] = {
            "requires_hitl": any((outcome.get("audit") or {}).get("requires_hitl") for outcome in outcomes),
            "issue_count": sum(int((outcome.get("audit") or {}).get("issue_count") or 0) for outcome in outcomes),
        }
    validated = ExtractResponse(**payload)
    return validated.model_dump()


async def deliver_workflow_result(final_payload: Dict[str, Any]) -> bool:
    """Deliver one workflow aggregate (a status query result) to the job store."""
    job_id = final_payload["job_id"]
    page_outcomes = final_payload["page_outcomes"]
    try:
        _validate_page_accounting(final_payload)
    except (KeyError, TypeError, ValueError) as exc:
        log.error("Invalid workflow result for job %s: %s", job_id, exc)
        return await _job_store.deliver_workflow_result(
            job_id, LANE_SCHEDULER,
            {"request_id": job_id, "documents": {}, "status": "failed",
             "error": "Incomplete or inconsistent workflow page result"},
        )
    failed = final_payload.get("status") == "failed" and not page_outcomes
    if failed:
        # Nothing was extractable at all: deliver a typed failure without
        # fabricating an empty successful document.
        result_payload = {
            "request_id": job_id,
            "documents": {},
        }
        outcome = await _job_store.deliver_workflow_result(
            job_id, LANE_SCHEDULER, {**result_payload, "status": "failed",
                                     "error": final_payload.get("error")},
        )
        return outcome

    result_payload = await build_result_payload(page_outcomes, job_id, final_payload["artifact_refs"])
    needs_review = final_payload.get("status") in {"needs_review", "waiting_for_human_review"}
    result_payload["status"] = final_payload.get("status")
    result_payload["error"] = final_payload.get("error")
    return await _job_store.deliver_workflow_result(
        job_id, LANE_SCHEDULER, result_payload, needs_review=needs_review,
    )


ACTIONABLE_DELIVERY_STATES = {"waiting_for_human_review", "completed", "partial", "failed", "needs_review"}


async def sync_one_workflow(job_id: str) -> Optional[bool]:
    """Query one lane workflow and deliver its state if actionable."""
    status = await _query_workflow_status(job_id)
    if status.get("status") not in ACTIONABLE_DELIVERY_STATES:
        return None
    return await deliver_workflow_result(status)


async def reconcile_pending_results(limit: int = 25) -> int:
    """Deliver results for submitted lanes whose workflow reached an actionable state."""
    delivered = 0
    for submitted in await _job_store.submitted_lane_jobs(LANE_SCHEDULER, limit=limit):
        try:
            await _job_store.set_processing_by_scheduler(submitted["job_id"], LANE_SCHEDULER)
            if await sync_one_workflow(submitted["job_id"]):
                delivered += 1
        except Exception as exc:
            log.warning(
                "Result delivery pending for job %s (%s: %s)",
                submitted["job_id"], type(exc).__name__, exc,
            )
    return delivered


async def reconcile_failed_review_waits(limit: int = 25) -> int:
    closed = 0
    for job_id in await _job_store.waiting_lane_jobs(LANE_SCHEDULER, limit=limit):
        try:
            status = await _query_workflow_status(job_id)
            if status.get("status") == "failed" and status.get("error") in {
                "review_timeout", "review_confirmation_failed",
            }:
                closed += bool(await _job_store.close_failed_lane_review(
                    job_id, LANE_SCHEDULER, timed_out=status["error"] == "review_timeout",
                ))
        except Exception as exc:
            log.warning("Review status pending for job %s (%s: %s)", job_id, type(exc).__name__, exc)
    return closed


async def _deliver_lane_review_signal(job_id: str, request: ReviewRequest) -> None:
    """Deliver an already-durable review to the waiting workflow.

    The review event is already authoritative in the job store; a missed
    signal never loses the decision — the reconciler re-delivers on the
    next pass when the workflow is still waiting.
    """
    submission = await _job_store.submission_for_job(job_id)
    if submission is None or not submission.get("workflow_id"):
        log.warning("Review recorded but no workflow is known yet for job %s", job_id)
        return
    # Only bounded review metadata crosses the workflow boundary; the
    # corrected document text itself lives only in the job store.
    signal_payload = {
        "result_version": request.result_version,
        "corrections": [
            {"start": c.start, "end": c.end, "replacement_length": len(c.replacement), "has_expected": c.expected is not None}
            for c in request.corrections
        ],
    }
    from .client import send_human_review_signal

    await send_human_review_signal(submission["workflow_id"], signal_payload)
    await _job_store.record_lane_review_signal(job_id, request.result_version + 1)


async def reconcile_pending_reviews(limit: int = 25) -> int:
    delivered = 0
    for pending in await _job_store.pending_lane_reviews(LANE_SCHEDULER, limit=limit):
        try:
            await _deliver_lane_review_signal(pending["job_id"], ReviewRequest.model_validate(pending["request"]))
            delivered += 1
        except Exception as exc:
            log.warning("Review signal pending for job %s (%s: %s)", pending["job_id"], type(exc).__name__, exc)
    return delivered


async def reconcile_pending_cancellations(limit: int = 25) -> int:
    from .client import cancel_ingestion_workflow

    delivered = 0
    for pending in await _job_store.pending_lane_cancellations(LANE_SCHEDULER, limit=limit):
        try:
            await cancel_ingestion_workflow(pending["workflow_id"])
            await _job_store.record_lane_cancellation(pending["job_id"])
            delivered += 1
        except Exception as exc:
            log.warning("Cancellation pending for job %s (%s: %s)", pending["job_id"], type(exc).__name__, exc)
    return delivered


async def reconcile_once() -> None:
    await reconcile_pending_submissions()
    await reconcile_pending_results()
    await reconcile_failed_review_waits()
    await reconcile_pending_reviews()
    await reconcile_pending_cancellations()


async def run_reconciler(interval_seconds: float = 2.0) -> None:
    """Lifespan-owned recovery loop; each pass is independently retryable."""
    while True:
        try:
            await reconcile_once()
        except Exception:
            log.exception("Temporal lane reconciliation pass failed")
        await asyncio.sleep(interval_seconds)
