# PaddleOCR Service (IndoLog) - Backend

This backend service provides OCR extraction capabilities for the IndoLog platform. It processes various document formats (PDFs, images, Office documents) using PaddleOCR and native parsers to extract structured text, layout information, and metadata.

## Architecture

```mermaid
graph TD
    A[Client Applications] --> B(IndoLog Backend)
    B --> C[PaddleOCR Service]
    C --> D[FastAPI API]
    C --> E[CLI Interface]
    D --> F[Async Job Processing]
    F --> G[SQLite Job Store]
    D --> H[OCR Extraction Pipeline]
    H --> I[PaddleOCR Engine]
    H --> J[Native Parsers (PDF, DOCX, HTML)]
    H --> K[Output Generation (JSON, MD)]
    G --> L[Job Persistence]
    I --> M[GPU/CPU Processing]
    style C fill:#f9f,stroke:#333,stroke-width:2px
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Web Framework | FastAPI |
| ASGI Server | Uvicorn |
| OCR Engine | PaddleOCR (PP-StructureV3) |
| Document Parsing | pdfplumber, python-docx, BeautifulSoup4, PyMuPDF |
| Job Store | SQLite (via aiosqlite) |
| Environment Management | python-dotenv |
| Async Processing | asyncio |
| Type Checking | None (but uses Pydantic models) |
| Containerization | Docker |
| Orchestration | Docker Compose |
| Deployment | Azure Container Apps (ACA) |
| Registry | Azure Container Registry (ACR) |

## Quick Start

```bash
# Clone the repository
git clone <repository-url>
cd paddleocr

# Copy environment template
cp .env.example .env   # or edit .env directly

# Build and run with Docker Compose (CPU)
docker compose -f docker-compose.cpu.yml up --build

# For GPU (if available)
docker compose -f docker-compose.yml up --build
```

The container binds to `http://127.0.0.1:8000`. `/health` is public; document routes require a service API key.

Generate a random key with at least 32 bytes of entropy and calculate its SHA-256 digest. Set `INGESTION_API_KEY_SHA256_JSON` in `.env` to a JSON object whose key is the digest and whose value is, for example, `{"tenant_id":"indolog","subject":"backend","roles":["extract"]}`. Send the original key as `Authorization: Bearer <key>`. Use a separate credential with the `review` role for reviewers. Store the original key in the caller's secret store; do not commit it. An absent or invalid credential mapping makes document routes return 503.

`POST /extract/v1` is the available extraction route. It accepts up to eight files, 15 MiB per file and 30 MiB in total, with a small multipart overhead allowance. Jobs are persisted in SQLite and claimed by a separate worker. Poll `GET /extract/v1/{job_id}`; the job owner can cancel with `DELETE /extract/v1/{job_id}`. The API and worker must use the same `INGESTION_OUTPUT_DIR` and SQLite database on one host. Both Compose examples start a worker. For a source checkout, start the API and, in another terminal, run `PYTHONPATH=src python -m ingestion_pipeline.services.worker`. The worker runs one OCR child at a time with a 20-minute deadline and retries a failed job once. Admission defaults to at most eight active jobs globally, four per tenant, 120 MiB reserved globally, 60 MiB per tenant, and 20 submissions per tenant per minute. Configure ceilings through the six `INGESTION_MAX_*` settings in [.env.example](.env.example), using representative host measurements; one running OCR job remains the hard limit.

The worker checks each document before extraction. A PDF may have at most 50 pages, with page sides up to 4000 points. Images may have at most 20 million pixels and one frame. DOCX/XLSX archives have member, expanded-size, and compression-ratio limits; spreadsheet and text parsers also cap rows, cells, blocks, and output size. The serialized result is limited to 8 MiB. Invalid, encrypted, unsupported, or wholly empty documents fail with a stable error code. A document with extracted text on some PDF pages and no text on others returns `partial`, with one-based `processed_pages` and `empty_pages`; the job status is also `partial`. OCR correction proposals appear in `correction_suggestions` while recognized `content` remains unchanged. A missing confidence score is `null`.

The durable job route accepts only the `legacy` engine; callbacks and OCR visualizations are disabled until their artifact budgets and delivery controls are implemented. Jobs with OCR correction suggestions or an explicit review flag enter `waiting_for_review`. A tenant reviewer can approve or correct one document at a time with `POST /extract/v1/{job_id}/review`, supplying `document_name`, `result_version`, and exact-span `corrections` (`start`, `end`, `expected`, `replacement`). A stale version or mismatched span returns 409; accepted changes and their before/after snapshots are recorded in SQLite. `GET /extract/v1/{job_id}/reviews` returns review metadata to tenant reviewers. Unreviewed results expire after seven days; other finished results expire after 24 hours. The worker removes input artifacts and expired records during its polling loop. Server-directory routes remain unavailable; synchronous extraction and LangGraph require explicit local feature opt-ins and are not production paths. The older Temporal submission path remains disabled. The SQLite queue is a single-host implementation; it is not a distributed job store and must not be used with independent API/worker filesystems.

`GET /health` is liveness. `GET /ready` checks the local SQLite store, recent worker heartbeat (10 seconds), and 256 MiB of free disk space. `GET /operations/metrics` requires an `admin` role and reports job counts, queue/review age, reserved bytes, retries, per-attempt duration and sampled process/GPU/artifact peaks, cleanup failures, worker heartbeat age, disk capacity, and active alert labels. CPU RSS is sampled from `/proc`; per-process GPU memory is sampled from `nvidia-smi` when available. OOM is counted only when a child receives SIGKILL and its cgroup-v2 OOM-kill counter increases; an unexplained SIGKILL is reported separately. Run the worker continuously for readiness, alert evaluation, and scheduled retention. See [retention and monitoring](docs/runbooks/retention-and-monitoring.md) for thresholds and backup requirements.

For local development, `.env` sets `ENCRYPTED_BACKUPS=false`. A production deployment should set it to `true` only with an actual encrypted SQLite backup and restore process; the flag alone does not make backups.

The optional LayoutReader requires `LAYOUTREADER_MODEL_DIR` to point to an existing absolute local model directory; it never downloads a model at runtime. The disabled Temporal client and webhook activity reject callback URLs before making a network request. The CPU and GPU Dockerfiles pin base-image digests and package locks. Their builds prefetch the active PP-StructureV3 models and verify them against [model-artifacts.json](model-artifacts.json). At runtime, the API and worker reject a missing or changed model bundle, and OCR uses explicit local model paths. Update that manifest only after reviewing a new model release and its source; recording hashes from an untrusted cache does not establish model provenance. See [Phase 5 progress](PHASE_5_PROGRESS.md) and [Phase 6 release evidence](docs/runbooks/phase06-release-evidence.md) for validation and remaining gates.

## Folder Structure

```
paddleocr/
├── src/
│   └── ingestion_pipeline/     # Main Python package
│       ├── api.py              # FastAPI application
│       ├── main.py             # CLI entry point
│       ├── config.py           # Configuration & environment variables
│       ├── extraction/         # OCR and document processing logic
│       ├── processing/         # Validation and post-processing
│       ├── schema/             # Pydantic models for requests/responses
│       ├── job_store.py        # SQLite-backed job store
│       └── core/               # Utilities and helpers
├── blob/                       # Input/output directory (mounted volume)
│   ├── input/                  # Default input directory
│   └── output/                 # Default output directory & job store
├── docker-compose.yml          # GPU-enabled compose file
├── docker-compose.cpu.yml      # CPU-only compose file
├── Dockerfile                  # Base Dockerfile
├── Dockerfile.gpu              # GPU-enabled Dockerfile
├── pyproject.toml              # Project dependencies and scripts
├── .env                        # Environment variables
├── README.md                   # This file
└── docs/                       # Documentation (see below)
```

## Environment Setup

The service uses environment variables configured via `.env` file. Key variables include:

- `INGESTION_INPUT_DIR`: Directory for input files (default: `blob/input`)
- `INGESTION_OUTPUT_DIR`: Directory for output and job store (default: `blob/output`)
- `INGESTION_API_KEY_SHA256_JSON`: Required credential digest-to-tenant/role mapping for document routes
- `PADDLE_USE_GPU`: Enable GPU acceleration (`true`/`false`)
- OCR settings: `OCR_DPI`, `OCR_ENHANCE`, `OCR_MAX_DIM`, `OCR_DTYPE`, `OCR_GRAYSCALE`
- PaddleOCR model settings: `LAYOUT_NMS`, `TEXT_DET_THRES`, etc.
- See [config.py](./src/ingestion_pipeline/config.py) for the full list.

## Running Tests

```bash
# With pytest installed in the project environment:
PYTHONPATH=src .venv/bin/python -m pytest -q
```

## Documentation

Detailed documentation is available in the `docs/` directory:

- [Phase 3 fixture validation](./PHASE_3_VALIDATION.md)

- [Architecture Overview](./docs/architecture/overview.md)
- [Service Documentation](./docs/services/ingestion-pipeline.md)
- [API Specification](./docs/api/openapi.yaml)
- [Database Schema](./docs/database/schema.md)
- [Runbooks](./docs/runbooks/)
  - [Local Setup](./docs/runbooks/local-setup.md)
  - [Deployment](./docs/runbooks/deployment.md)
  - [Troubleshooting](./docs/runbooks/troubleshooting.md)
- [Architecture Decision Records](./docs/adr/)


## Changelog

See [CHANGELOG.md](./docs/CHANGELOG.md) for a list of changes.

---

*Built with ❤️ for the IndoLog platform.*
