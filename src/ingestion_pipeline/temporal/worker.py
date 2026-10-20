"""
Temporal Activity & Workflow Worker Daemon
------------------------------------------
Runs two segregated task queues:
1. 'gpu-ocr-tasks': Throttled to max_concurrent_activities=1 to guarantee zero CUDA OOM on 12GB GPU.
2. 'cpu-document-tasks': High concurrency worker for PDF parsing, OpenCV tables, and webhooks.

Usage:
    uv run python -m ingestion_pipeline.temporal.worker --server localhost:7233
"""

import argparse
import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import (
    CPU_QUEUE,
    GPU_QUEUE,
    audit_page_activity,
    confirm_review_activity,
    digital_page_activity,
    dispatch_webhook_activity,
    image_page_activity,
    native_document_activity,
    plan_document_activity,
    route_page_activity,
    scanned_page_activity,
)
from .workflows import (
    DocumentIngestionWorkflow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("temporal.worker")


def prewarm_models() -> None:
    """Pre-warm GPU OCR models on worker startup so request #1 is served at warm speed."""
    log.info("Pre-warming GPU PaddleOCR into VRAM...")
    try:
        import numpy as np

        from ..extraction.decoupled_pipeline import extract_pure_paddle_ocr
        dummy = np.zeros((100, 100, 3), dtype=np.uint8)
        extract_pure_paddle_ocr(dummy)
        log.info("PaddleOCR pre-warmed successfully!")
    except Exception as e:
        log.warning("Could not pre-warm OCR: %s", e)


async def run_worker(server_addr: str = "localhost:7233", namespace: str = "default") -> None:
    """Connect to Temporal and start the CPU and GPU workers."""
    log.info("Connecting to Temporal server at %s (namespace: %s)...", server_addr, namespace)
    client = await Client.connect(server_addr, namespace=namespace)

    # 1. Pre-warm GPU models into VRAM
    prewarm_models()

    # 2. Worker for CPU Tasks (High Concurrency: 16)
    cpu_worker = Worker(
        client,
        task_queue=CPU_QUEUE,
        workflows=[DocumentIngestionWorkflow],
        activities=[
            plan_document_activity,
            route_page_activity,
            audit_page_activity,
            confirm_review_activity,
            digital_page_activity,
            native_document_activity,
            dispatch_webhook_activity,
        ],
        max_concurrent_activities=16,
        # A waiting review must be queryable promptly after this worker
        # restarts; disable sticky caches for this bounded-metadata lane.
        max_cached_workflows=0,
    )

    # 3. Worker for GPU Tasks (Strict Throttling: Concurrency = 1)
    # Ensures ONLY ONE page touches the 12GB GPU at a time. Zero OOM risk!
    gpu_worker = Worker(
        client,
        task_queue=GPU_QUEUE,
        activities=[scanned_page_activity, image_page_activity],
        max_concurrent_activities=1,
    )

    log.info("Temporal Workers initialized:")
    log.info("  Queue '%s' active (max_concurrent_activities: 16)", CPU_QUEUE)
    log.info("  Queue '%s' active (max_concurrent_activities: 1 - GPU VRAM PROTECTED)", GPU_QUEUE)
    log.info("Listening for incoming workflows and activities...")

    # Run both workers concurrently
    await asyncio.gather(cpu_worker.run(), gpu_worker.run())


def main() -> None:
    parser = argparse.ArgumentParser(description="Temporal Ingestion Worker")
    parser.add_argument("--server", type=str, default="localhost:7233", help="Temporal server address")
    parser.add_argument("--namespace", type=str, default="default", help="Temporal namespace")
    args = parser.parse_args()

    try:
        asyncio.run(run_worker(server_addr=args.server, namespace=args.namespace))
    except KeyboardInterrupt:
        log.info("Worker stopped by user.")


if __name__ == "__main__":
    main()
