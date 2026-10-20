"""Temporal client for the extraction test lane (Phase 02).

Repairs applied per the Phase 02 spec:
- ``start_workflow`` uses the supported ``args=[...]`` form instead of
  passing two positional workflow arguments.
- Connection host/port and namespace come from configured environment
  values, never from an untrusted caller; the dev CLI keeps its explicit
  flags but the production path does not.
- Workflow ID is derived from the server-generated scheduler job ID, never
  from a source filename or local path.
- Callback delivery stays disabled end to end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any, Dict

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from .workflows import CPU_TASK_QUEUE, DocumentIngestionWorkflow

log = logging.getLogger("temporal.client")

CPU_QUEUE = CPU_TASK_QUEUE  # public alias retained for tests and tooling


def configured_connection() -> tuple[str, str]:
    """Server address/namespace come from environment configuration only."""
    address = os.getenv("INGESTION_TEMPORAL_ADDR", "localhost:7233")
    namespace = os.getenv("INGESTION_TEMPORAL_NAMESPACE", "default")
    if not address or ":" not in address:
        raise ValueError("INGESTION_TEMPORAL_ADDR must be host:port")
    return address, namespace


def _workflow_id(job_id: str) -> str:
    if not job_id.strip() or len(job_id) > 80 or any(ch in job_id for ch in "/\\ \t"):
        raise ValueError("Workflow ID must derive from a bounded server job ID")
    return f"doc-extract-{job_id}"


async def start_ingestion_workflow(job_request: Dict[str, Any]) -> str:
    """Submit one admitted job to the isolated test-lane workflow.

    The request was validated and staged at admission; this call must be
    idempotent from the caller's perspective: the workflow ID is derived
    from the server job ID, so resubmitting the same job joins the same
    workflow instead of creating a duplicate.
    """
    job_id = str(job_request.get("job_id") or "")
    wf_id = _workflow_id(job_id)
    if job_request.get("callback_url"):
        raise ValueError("Callback delivery is disabled")

    address, namespace = configured_connection()
    log.info("Connecting to Temporal %s (namespace=%s)", address, namespace)
    client = await Client.connect(address, namespace=namespace)

    log.info("Starting ingestion workflow '%s' for job '%s'", wf_id, job_id)
    try:
        handle = await client.start_workflow(
            DocumentIngestionWorkflow.run,
            args=[dict(job_request)],
            id=wf_id,
            task_queue=CPU_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        # Crash-recovery replay or duplicate admission: the deterministic ID
        # means the caller joins the already-running execution instead of
        # forking a second one. One logical run per admitted job.
        log.info("Workflow '%s' already started; joining the existing execution", wf_id)
        return wf_id
    log.info("Workflow '%s' submitted successfully", handle.id)
    return handle.id


async def get_workflow_status(workflow_id: str) -> Dict[str, Any]:
    address, namespace = configured_connection()
    client = await Client.connect(address, namespace=namespace)
    handle = client.get_workflow_handle(workflow_id)
    return await handle.query(DocumentIngestionWorkflow.get_status)


async def send_human_review_signal(workflow_id: str, review_data: Dict[str, Any]) -> None:
    """Deliver an already-authorized review decision to the waiting workflow."""
    address, namespace = configured_connection()
    client = await Client.connect(address, namespace=namespace)
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(DocumentIngestionWorkflow.submit_human_review, review_data)


async def cancel_ingestion_workflow(workflow_id: str) -> None:
    address, namespace = configured_connection()
    client = await Client.connect(address, namespace=namespace)
    await client.get_workflow_handle(workflow_id).cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description="Temporal ingestion client (test lane)")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="Submit a staged job request")
    submit.add_argument("--job-request", type=str, required=True, help="Path to the validated JobRequest JSON")

    status_p = sub.add_parser("status", help="Query a workflow")
    status_p.add_argument("workflow_id", type=str)

    review_p = sub.add_parser("review", help="Deliver an authorized review signal (dev tool)")
    review_p.add_argument("workflow_id", type=str)
    review_p.add_argument("--corrections-json", type=str, required=True)

    args = parser.parse_args()
    if args.command == "submit":
        job_request = json.loads(open(args.job_request, encoding="utf-8").read())
        handle_id = asyncio.run(start_ingestion_workflow(job_request))
        print(handle_id)
    elif args.command == "status":
        result = asyncio.run(get_workflow_status(args.workflow_id))
        print(json.dumps(result, indent=2))
    else:
        asyncio.run(send_human_review_signal(args.workflow_id, json.loads(args.corrections_json)))


if __name__ == "__main__":
    main()
