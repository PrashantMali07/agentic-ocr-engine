"""
Temporal Workflow Definition for the Resilient Document Ingestion Test Lane
---------------------------------------------------------------------------
Design (Phases 01/02 contracts):

1. The workflow receives a small JobRequest (server-generated job ID,
   admitted document IDs) — never a caller-controlled file path.
2. A planning activity classifies every page once (complete manifest);
   each page is then processed by exactly one activity on the correct
   queue: CPU for digital/native work, the GPU-throttled queue for scanned
   pages and images.
3. Activities return small metadata only (statuses, artifact IDs, hashes,
   confidence, audit summary). Raw OCR text, tokens, tables, and images
   never enter workflow history.
4. Every planned page produces exactly one bounded terminal outcome
   (completed/empty/failed); missing pages make the aggregate explicitly
   failed instead of silently partial.
5. Native HITL survives restart: waiting_for_review pauses the workflow
   through wait_condition up to the review TTL; the review signal stays
   authorized upstream (API), never from the workflow.
"""

from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# Import activity implementations (unwrapped in the workflow sandbox)
with workflow.unsafe.imports_passed_through():
    from .activities import (
        CPU_QUEUE,
        GPU_QUEUE,
        audit_page_activity,
        confirm_review_activity,
        digital_page_activity,
        image_page_activity,
        native_document_activity,
        plan_document_activity,
        route_page_activity,
        scanned_page_activity,
    )

CPU_TASK_QUEUE = CPU_QUEUE
GPU_TASK_QUEUE = GPU_QUEUE

# Keep the legacy public aliases so existing integrations keep importing.
CPU_QUEUE_EXPORT = CPU_QUEUE
GPU_QUEUE_EXPORT = GPU_QUEUE
DocumentIngestionWorkflow_NAME = "DocumentIngestionWorkflow"

RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

DIGITAL_TIMEOUT = timedelta(minutes=2)
GPU_PAGE_TIMEOUT = timedelta(minutes=10)
NATIVE_TIMEOUT = timedelta(minutes=5)
PLAN_TIMEOUT = timedelta(minutes=2)
REVIEW_TTL = timedelta(days=7)


# The workflow body is deterministic control flow over metadata only — every
# filesystem/GPU side effect lives in activities, outside the sandbox. The
# sandbox's restricted importer nonetheless re-executes heavy module imports
# (extraction config renders absolute paths) that this deployment needs at
# import time, so this workflow opts out of the sandbox explicitly. Activity
# code remains fully sandbox-independent by design.
@workflow.defn(sandboxed=False)
class DocumentIngestionWorkflow:
    def __init__(self) -> None:
        self.status = "initialized"
        self.current_stage = "plan"
        self.human_review_received = False
        self.human_corrections: Optional[Dict[str, Any]] = None
        self.review_confirmed = False
        self.page_outcomes: List[Dict[str, Any]] = []
        self.final_payload: Dict[str, Any] = {}

    @workflow.signal
    def submit_human_review(self, review_data: Dict[str, Any]) -> None:
        """Review is an authorized API event; the workflow only observes it."""
        if (self.status == "waiting_for_human_review"
                and isinstance(review_data.get("result_version"), int)
                and review_data["result_version"] >= 0):
            self.human_corrections = {"result_version": review_data["result_version"]}
            self.human_review_received = True

    @workflow.query
    def get_status(self) -> Dict[str, Any]:
        return {
            **self.final_payload,
            "status": self.status,
            "stage": self.current_stage,
            "page_outcomes": list(self.page_outcomes),
            "human_review_received": self.human_review_received,
        }

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _validate_plan(plan: List[Dict[str, Any]]) -> None:
        pages: Dict[str, set[int]] = {}
        totals: Dict[str, int] = {}
        for page in plan:
            document_id = page["document_id"]
            number = page["page_number"]
            total = page["total_pages"]
            if (type(number) is not int or type(total) is not int or
                    not 1 <= number <= total <= 10_000):
                raise ValueError("Invalid planned page identity")
            seen = pages.setdefault(document_id, set())
            if number in seen or (document_id in totals and totals[document_id] != total):
                raise ValueError("Duplicate or inconsistent planned page")
            seen.add(number)
            totals[document_id] = total
        if any(seen != set(range(1, totals[document_id] + 1))
               for document_id, seen in pages.items()):
            raise ValueError("Planned document is missing declared pages")

    @staticmethod
    def _validate_outcome(page: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        for field in ("document_id", "display_name", "extension", "page_number", "total_pages", "classification"):
            if outcome.get(field) != page[field]:
                raise ValueError(f"Page activity returned mismatched {field}")
        if outcome.get("status") not in {"completed", "empty", "failed", "needs_review"}:
            raise ValueError("Page activity returned unknown status")

    def _dispatch(self, page: Dict[str, Any], job_id: str, action: str) -> Any:
        if action not in {"native", "digital", "gpu"}:
            raise ValueError("Invalid bounded extraction route")
        payload = {
            "job_id": job_id,
            "document_id": page["document_id"],
            "display_name": page["display_name"],
            "extension": page["extension"],
            "page_number": page["page_number"],
            "total_pages": page["total_pages"],
        }
        if action == "native":
            return workflow.execute_activity(
                native_document_activity, payload,
                task_queue=CPU_TASK_QUEUE,
                start_to_close_timeout=NATIVE_TIMEOUT,
                retry_policy=RETRY_POLICY,
            )
        if action == "digital":
            return workflow.execute_activity(
                digital_page_activity, payload,
                task_queue=CPU_TASK_QUEUE,
                start_to_close_timeout=DIGITAL_TIMEOUT,
                retry_policy=RETRY_POLICY,
            )
        return workflow.execute_activity(
            scanned_page_activity if page["extension"] == "pdf" else image_page_activity,
            payload,
            task_queue=GPU_TASK_QUEUE,
            start_to_close_timeout=GPU_PAGE_TIMEOUT,
            retry_policy=RETRY_POLICY,
        )

    @workflow.run
    async def run(self, job_request: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(job_request["job_id"])
        if not job_id or len(job_id) > 80:
            raise ValueError("Job identity must be a bounded server-generated ID")

        # --------------------------------------------------------- Step 1: plan
        self.status = "running"
        self.current_stage = "plan"
        try:
            plan = await workflow.execute_activity(
                plan_document_activity, job_request,
                task_queue=CPU_TASK_QUEUE,
                start_to_close_timeout=PLAN_TIMEOUT,
                retry_policy=RETRY_POLICY,
            )
        except Exception as exc:
            self.status = "failed"
            self.current_stage = "completed"
            self.final_payload = {"job_id": job_id, "status": "failed", "page_outcomes": [],
                                  "artifact_refs": [], "error": f"Planning failed: {type(exc).__name__}"}
            return self.final_payload
        if not plan:
            self.status = "failed"
            self.current_stage = "completed"
            self.final_payload = {"job_id": job_id, "status": "failed", "page_outcomes": [],
                                  "artifact_refs": [], "error": "Planning returned no pages"}
            return self.final_payload
        try:
            self._validate_plan(plan)
        except (KeyError, TypeError, ValueError) as exc:
            self.status = "failed"
            self.current_stage = "completed"
            self.final_payload = {"job_id": job_id, "status": "failed", "page_outcomes": [],
                                  "artifact_refs": [], "error": f"Invalid page plan: {type(exc).__name__}"}
            return self.final_payload

        # --------------------------------------------------------- Step 2..n: pages
        self.current_stage = "page_extraction"
        self.page_outcomes = []
        errors: List[str] = []
        for page in plan:
            try:
                route = await workflow.execute_activity(
                    route_page_activity, page, task_queue=CPU_TASK_QUEUE,
                    start_to_close_timeout=PLAN_TIMEOUT, retry_policy=RETRY_POLICY,
                )
                expected_route = (
                    "native" if page["extension"] in {"docx", "html", "htm", "xlsx", "xls", "csv"}
                    else "digital" if page["classification"] == "digital" else "gpu"
                )
                if route.get("action") != expected_route:
                    raise ValueError("Graph selected an incompatible extraction route")
                outcome = await self._dispatch(page, job_id, route["action"])
                self._validate_outcome(page, outcome)
                decision = await workflow.execute_activity(
                    audit_page_activity, outcome, task_queue=CPU_TASK_QUEUE,
                    start_to_close_timeout=PLAN_TIMEOUT, retry_policy=RETRY_POLICY,
                )
                if decision.get("action") not in {"accept", "review", "fail"}:
                    raise ValueError("Graph returned an unknown audit action")
                outcome["decision"] = decision
                if decision["action"] == "review":
                    outcome["audit"] = {**(outcome.get("audit") or {}), "requires_hitl": True}
                elif decision["action"] == "fail":
                    outcome["status"] = "failed"
            except Exception as exc:  # activity failures after retries
                outcome = {
                    "document_id": page["document_id"],
                    "display_name": page["display_name"],
                    "extension": page["extension"],
                    "page_number": page["page_number"],
                    "total_pages": page["total_pages"],
                    "classification": page["classification"],
                    "status": "failed",
                    "error": [type(exc).__name__, "Activity retries exhausted"],
                    "audit": {"is_valid": False, "requires_hitl": True, "issue_count": 1},
                }
                errors.append(f"{page['document_id']}:p{page['page_number']}")
            self.page_outcomes.append(outcome)

        # --------------------------------------------------------- Step n+1: aggregate
        self.current_stage = "aggregate"
        statuses = [outcome.get("status") for outcome in self.page_outcomes]
        planned_pages = len(plan)
        # An accountented empty page is a terminal outcome (missing text, explicit
        # empty_pages in the legacy shape) — it maps to partial, never silent
        # success and never a lost page. Only activity failures (status=failed)
        # and unaccounted pages are failures.
        delivered = sum(1 for status in statuses if status in {"completed", "needs_review", "empty"})
        any_failed = any(status == "failed" for status in statuses)
        needs_review = any(outcome.get("audit", {}).get("requires_hitl") for outcome in self.page_outcomes)
        any_empty = any(status == "empty" for status in statuses)
        page_confidences = [
            outcome["confidence"] for outcome in self.page_outcomes
            if isinstance(outcome.get("confidence"), (int, float))
        ]
        if delivered != planned_pages and not any_failed:
            # Defensive: a lost page outcome must never present as success.
            errors.append(f"planned={planned_pages}, delivered={delivered}")
            any_failed = True
        overall = "failed" if any_failed else (
            "needs_review" if needs_review else ("partial" if any_empty else "completed")
        )

        self.status = "waiting_for_human_review" if overall == "needs_review" else overall
        artifact_refs = [
            {"artifact_id": outcome["text_artifact"], "document_id": outcome["document_id"],
             "page_number": outcome["page_number"], "sha256": outcome.get("text_artifact_sha256")}
            for outcome in self.page_outcomes
            if outcome.get("text_artifact")
        ]
        self.final_payload = {
            "job_id": job_id, "status": overall, "pages": planned_pages,
            "delivered_pages": delivered, "page_outcomes": list(self.page_outcomes),
            "artifact_refs": artifact_refs,
            "avg_confidence": (round(sum(page_confidences) / len(page_confidences), 4)
                                if page_confidences else None),
            "error": "; ".join(errors) if errors else None,
            "engine_used": "shared_service" if any(o.get("classification") == "scanned" for o in self.page_outcomes) else "digital_cpu",
            "human_reviewed": False,
        }
        if overall == "needs_review":
            self.current_stage = "hitl_paused"
            # Signals are only wakeups. The committed review event is checked
            # through a CPU activity before the workflow accepts completion.
            try:
                review_deadline = workflow.now() + REVIEW_TTL
                while True:
                    remaining = review_deadline - workflow.now()
                    if remaining <= timedelta(0):
                        raise TimeoutError("Review deadline elapsed")
                    await workflow.wait_condition(lambda: self.human_review_received, timeout=remaining)
                    review = self.human_corrections or {}
                    self.human_review_received = False
                    accepted = await workflow.execute_activity(
                        confirm_review_activity,
                        {"job_id": job_id, "result_version": review["result_version"]},
                        task_queue=CPU_TASK_QUEUE, start_to_close_timeout=PLAN_TIMEOUT,
                        retry_policy=RETRY_POLICY,
                    )
                    if accepted:
                        self.review_confirmed = True
                        break
                overall = "partial" if any_empty else "completed"
                self.status = overall
                self.current_stage = "completed"
            except TimeoutError:
                overall = "failed"
                self.status = "failed"
                errors.append("review_timeout")
            except Exception:
                overall = "failed"
                self.status = "failed"
                errors.append("review_confirmation_failed")
        else:
            self.current_stage = "completed"
        self.final_payload["status"] = overall
        self.final_payload["error"] = "; ".join(errors) if errors else None
        self.final_payload["human_reviewed"] = self.review_confirmed
        return self.final_payload
