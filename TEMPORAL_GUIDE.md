# Temporal Orchestration & Hybrid Ingestion Architecture Guide

> The Temporal path is inactive and is not the production review workflow. Use the durable SQLite worker and `/extract/v1/{job_id}/review` route documented in README.md.

This document describes how to run, configure, and operate the **Temporal Orchestration Layer** and the **Hybrid Decoupled Extraction Pipeline** within the LogisticAI Ingestion Platform.

---

## 1. Architecture Overview

```
                      +-----------------------------+
                      | Client / API Gateway / ERP  |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |      Temporal Cluster       |
                      |       (localhost:7233)      |
                      +--------------+--------------+
                                     |
            +------------------------+------------------------+
            |                                                 |
            v                                                 v
+-------------------------------+             +-------------------------------+
|  Queue: 'cpu-document-tasks'  |             |     Queue: 'gpu-ocr-tasks'    |
|   (Concurrency = 16 workers)  |             |    (Concurrency = 1 worker)   |
|                               |             |                               |
| • PDF Triage & Deskew         |             | • Pure PaddleOCR Inference    |
| • OpenCV Table Structuring    |             |   (Locked to 1 job at a time; |
| • Reading Order Sorting       |             |    protects 12 GB GPU VRAM)   |
| • Auditor QA & Math Checks    |             +-------------------------------+
| • Pytesseract Fallback        |
| • Webhook Dispatching         |
+-------------------------------+
```

### Why Segregated Queues?
On a **12 GB VRAM GPU** (NVIDIA RTX 2060):
- Running multiple GPU inference tasks concurrently leads to CUDA Out-Of-Memory (OOM) crashes.
- The **`gpu-ocr-tasks`** queue is strictly capped at **`max_concurrent_activities = 1`**.
- The **`cpu-document-tasks`** queue scales up to **16 concurrent workers** for CPU-bound tasks (PDF parsing, deskewing, table clustering, webhooks).

---

## 2. Key Components

| Component | File Location | Purpose |
| :--- | :--- | :--- |
| **Workflow** | `src/ingestion_pipeline/temporal/workflows.py` | Defines `DocumentIngestionWorkflow` with multi-queue routing, retry policies, and native HITL signal pauses. |
| **Activities** | `src/ingestion_pipeline/temporal/activities.py` | 6 discrete activities partitioned across CPU and GPU queues. |
| **Worker Daemon** | `src/ingestion_pipeline/temporal/worker.py` | Listens on both queues, enforces concurrency limits, and pre-warms models into VRAM. |
| **Client / CLI** | `src/ingestion_pipeline/temporal/client.py` | Utilities to start workflows, query real-time status, and send human review signals. |
| **Test Harness** | `scripts/test_temporal_workflow.py` | Self-contained integration test using an ephemeral in-process Temporal test server. |

---

## 3. How to Use the Temporal Layer (Phase 02 test lane)

### Step 1: Run the integration tests
Real workflow + workers without an external server (time-skipping environment):

```bash
uv run python -m pytest tests/integration/test_temporal_lane.py -q
uv run python -m pytest tests/test_lane_bridge.py -q
```

These cover: digital fast path, mixed PDF page accounting, missing-page failure semantics,
review pause/resume, outbox reconciliation, guarded result delivery, and review notify.

---

### Step 2: Start the Worker Daemon

```bash
# Connect to local Temporal server (default: localhost:7233; configured via
# INGESTION_TEMPORAL_ADDR / INGESTION_TEMPORAL_NAMESPACE)
uv run python -m ingestion_pipeline.temporal.worker --server localhost:7233
```

On startup, the worker automatically:
1. Connects to the Temporal cluster (default namespace).
2. Pre-warms the PaddleOCR model into GPU VRAM.
3. Listens on `cpu-document-tasks` (concurrency: 16) — planning, digital/native pages.
4. Listens on `gpu-ocr-tasks` (concurrency: 1) — scanned pages and images only,
   one page at a time (render → OCR → reconcile via the Phase 01 shared service).

Payload rule: workflow history carries only small metadata (statuses, artifact IDs,
SHA-256, confidence, bounded audit summaries). Page text/tables/images live in the
job's artifact tree, referenced by deterministic artifact ID (retries rewrite in place).

---

### Step 3: Trigger a Document Extraction Workflow

General admission stays on the legacy route. The test lane is driven through the
bridge: admission writes the job + outbox row, and the reconciler starts (or joins)
the workflow under the deterministic ID `doc-extract-<job_id>` (always derived from
the server-generated job ID, never a filename or local path):

```python
import asyncio
from ingestion_pipeline.temporal import bridge

job_request = {
    "job_id": "<server-generated-job-id>",
    "documents": [
        {"document_id": "<hex32>", "extension": ".pdf", "display_name": "RFQ_06_scanned.pdf"}
    ],
}
workflow_id = asyncio.run(bridge.stage_and_reconcile(job_request))
```

Manual CLI (dev tool; requires a staged job request JSON):

```bash
uv run python -m ingestion_pipeline.temporal.client submit --job-request /path/to/request.json
```

Callers cannot choose a Temporal server/namespace/path; callbacks remain disabled
end to end and are rejected before any connection.

---

### Step 4: Deliver Results to Job Polling

`/extract/v1/{job_id}` polls the job store. The bridge (run per pass) moves lane
workflow state into the store with guarded, idempotent writes:

```python
import asyncio
from ingestion_pipeline.temporal import bridge

delivered = asyncio.run(bridge.reconcile_pending_results())  # returns number delivered
```

Delivery handles review-pause (`waiting_for_review`, provisional result), terminal
completion, and typed failures. It never overwrites a reviewed state and never claims
legacy-scheduler jobs.

---

### Step 5: Human-in-the-Loop (HITL) Review & Signal

Review authorization stays with the API. Submit reviews through the existing
endpoint; the durable review is recorded first, and the lane notifier then delivers
a bounded signal (result version + correction metadata only — never corrected text)
to the waiting workflow:

```bash
POST /extract/v1/{job_id}/review   # reviewer-authenticated; once per result version
```

The workflow pauses with zero CPU/GPU consumption for up to 7 days. A missed signal
never loses the review: recorded reviews remain authoritative and are reconcilable.

---

## 4. Programmatic Python Usage

```python
import asyncio
from ingestion_pipeline.temporal.client import start_ingestion_workflow, get_workflow_status

# job_request is validated and staged at admission; blocks/artifacts live under
# jobs/<job_id>/attachments keyed by hex32 document IDs.
workflow_id = asyncio.run(start_ingestion_workflow(job_request))
status = asyncio.run(get_workflow_status(workflow_id))
```

Lane selection for general traffic remains disabled until the Phase 06 acceptance
gates pass; the legacy SQLite worker continues to own `legacy` jobs throughout.

