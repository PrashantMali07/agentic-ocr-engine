# Ingestion Pipeline Service

## Purpose

The Ingestion Pipeline service provides optical character recognition (OCR) and document processing capabilities for the IndoLog platform. It converts various document formats (scanned PDFs, images, Office documents, HTML) into structured, machine-readable text with layout analysis, language detection, and confidence scoring.

## Owner

Backend Team ( IndoLog Platform )

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | Health check service status and configuration |
| POST | `/extract` | None | Synchronous extraction of uploaded documents |
| POST | `/extract/v1` | None | Asynchronous job submission for document extraction |
| GET | `/extract/v1/{job_id}` | None | Retrieve status/result of async extraction job |
| POST | `/extract-dir` | None | Process documents from a server directory path |
| POST | `/validate-attachments` | None | Validate extracted text against original documents |
| POST | `/batch-extract` | None | Process multiple directories in a single request |

*Note: Authentication is handled at the platform level (network policies, Azure AD). The service itself does not implement authentication.*

## Request/Response Examples

### Synchronous Extraction (`/extract`)

**Request:**
```http
POST /extract HTTP/1.1
Content-Type: multipart/form-data

-----------------------------boundary
Content-Disposition: form-data; name="request_id"

req-123
-----------------------------boundary
Content-Disposition: form-data; name="files"; filename="document.pdf"
Content-Type: application/pdf

[binary PDF data]
-----------------------------boundary--
```

**Response:**
```json
{
  "request_id": "req-123",
  "documents": {
    "document.pdf": {
      "content": "Extracted text from document...",
      "avg_confidence": 0.92,
      "format": "pdf",
      "pages": 5,
      "language": "en"
    }
  }
}
```

### Asynchronous Extraction (`/extract/v1`)

**Request:**
```http
POST /extract/v1 HTTP/1.1
Content-Type: multipart/form-data

-----------------------------boundary
Content-Disposition: form-data; name="files"; filename="scan.jpg"
Content-Type: image/jpeg

[binary image data]
-----------------------------boundary--
```

**Response (202 Accepted):**
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-g1h2-i3j4k5l6m7n8",
  "status": "queued",
  "created_at": "2026-09-30T10:00:00Z",
  "updated_at": "2026-09-30T10:00:00Z"
}
```

**Polling Response (`GET /extract/v1/{job_id}`):**
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-g1h2-i3j4k5l6m7n8",
  "status": "completed",
  "created_at": "2026-09-30T10:00:00Z",
  "updated_at": "2026-09-30T10:00:05Z",
  "result": {
    "request_id": "a1b2c3d4-e5f6-7890-g1h2-i3j4k5l6m7n8",
    "documents": {
      "scan.jpg": {
        "content": "Extracted text from image...",
        "avg_confidence": 0.87,
        "format": "jpg",
        "pages": 1,
        "language": "en"
      }
    }
  },
  "progress": {
    "total": 1,
    "processed": 1
  }
}
```

### Validation (`/validate-attachments`)

**Request:**
```http
POST /validate-attachments HTTP/1.1
Content-Type: multipart/form-data

-----------------------------boundary
Content-Disposition: form-data; name="files"; filename="document.pdf"
Content-Type: application/pdf

[binary PDF data]
-----------------------------boundary
Content-Disposition: form-data; name="include_diff"

true
-----------------------------boundary--
```

**Response:**
```json
{
  "request_id": "req-456",
  "results": [
    {
      "filename": "document.pdf",
      "valid": true,
      "format": "pdf",
      "size_bytes": 102400,
      "pages": 2,
      "errors": null,
      "diff": null
    }
  ],
  "total_files": 1,
  "valid_count": 1,
  "invalid_count": 0
}
```

## Dependencies

### Internal Components
- **Extraction Manager** (`src/ingestion_pipeline/extraction/manager.py`): Core OCR and document processing logic
- **Job Store** (`src/ingestion_pipeline/job_store.py`): SQLite-backed asynchronous job processing
- **Validation Module** (`src/ingestion_pipeline/processing/validation.py`): Text comparison and validation
- **Configuration** (`src/ingestion_pipeline/config.py`): Environment variable management
- **Schema Definitions** (`src/ingestion_pipeline/schema/`): Pydantic models for API requests/responses

### External Dependencies
- **PaddleOCR**: OCR engine for scanned documents
- **pdfplumber/PyMuPDF**: Digital PDF text extraction
- **python-docx**: DOCX text extraction
- **BeautifulSoup4/lxml**: HTML text extraction
- **langdetect**: Language detection
- **aiosqlite**: Async SQLite for job persistence
- **FastAPI/Uvicorn**: Web framework and server
- **python-multipart**: Handle file uploads
- **openpyxl/xlrd**: Excel file reading
- **msoffcrypto-tool**: Decrypt password-protected Office files
- **onnxruntime-gpu**: GPU acceleration for ONNX models
- **paddlepaddle-gpu**: PaddlePaddle framework with CUDA

### Databases
- **SQLite**: Job store persistence (file-based, zero administration)
  - Tables: `jobs` (job_id, data, updated_at)
  - Located at: `blob/output/jobs.db`

### External Services
- None (self-contained service)
- Relies on platform-provided storage (Azure Blob Storage mounted as volume) for input/output

## Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `INGESTION_INPUT_DIR` | Directory for input files | `blob/input` | No |
| `INGESTION_OUTPUT_DIR` | Directory for output and job store | `blob/output` | No |
| `PADDLE_USE_GPU` | Enable GPU acceleration | `true` | No |
| `OCR_DPI` | DPI for rendering PDF pages | `100` | Yes* |
| `OCR_ENHANCE` | Apply image enhancement | `true` | No |
| `OCR_MAX_DIM` | Max image dimension (px) | `4000` | No |
| `OCR_DTYPE` | Data type for OCR (`uint8`, `float16`, `float32`) | `uint8` | No |
| `OCR_GRAYSCALE` | Convert to grayscale before OCR | `false` | No |
| `LOW_DPI` | Low DPI threshold for PDF preprocessing | `100` | Yes* |
| `HIGH_DPI` | High DPI threshold for PDF preprocessing | `250` | Yes* |
| `LOW_MAX` | Low max dimension threshold | `3000` | Yes* |
| `HIGH_MAX` | High max dimension threshold | `4000` | Yes* |
| `LARGE_PDF_SIZE_BYTES` | Size threshold for large PDFs | `8000000` | Yes* |
| `LARGE_PDF_PAGE_COUNT` | Page count threshold for large PDFs | `10` | Yes* |
| `LAYOUT_NMS` | Enable layout non-maximum suppression | `true` | No |
| `LAYOUT_UNCLIP_RATIO` | Layout unclip ratio | `1.1` | No |
| `LAYOUT_MERGE_BBOXES_MODE` | Layout merge bboxes mode | `large` | No |
| `TEXT_DET_THRES` | Text detection threshold | `0.2` | No |
| `TEXT_DET_BOX_THRES` | Text detection box threshold | `0.4` | No |
| `TEXT_DET_UNCLIP_RATIO` | Text detection unclip ratio | `1.8` | No |
| `TEXT_DET_LIMIT_SIDE_LEN_PDF` | Text detection side limit for PDF | `1280` | No |
| `TEXT_DET_LIMIT_SIDE_LEN_IMAGE` | Text detection side limit for image | `1600` | No |
| `TEXT_DET_LIMIT_TYPE` | Text detection limit type | `max` | No |
| `TEXT_REC_SCORE_THRES` | Text recognition score threshold | `0.5` | No |
| `USE_DOC_ORIENTATION_CLASSIFY` | Use document orientation classification | `true` | No |
| `USE_DOC_UNWRAPPING` | Use document unwrapping | `true` | No |
| `USE_TABLE_RECOGNITION` | Use table recognition | `true` | No |
| `PYMUPDF_PRO_KEY` | PyMuPDF commercial license key | (empty) | No |

*Note: Variables marked with * are required and must be set in .env or environment.*

## Error Codes & Handling

The service uses HTTP status codes to indicate outcomes:

| Code | Description | Scenario |
|------|-------------|----------|
| 200 | OK | Successful synchronous request |
| 202 | Accepted | Async job submitted successfully |
| 400 | Bad Request | Invalid request (missing files, unsupported format, validation errors) |
| 404 | Not Found | Job ID not found or expired |
| 500 | Internal Server Error | Unexpected error during processing |
| 503 | Service Unavailable | GPU not available when required (rare) |

Error responses follow a consistent format:
```json
{
  "detail": "Human-readable error message"
}
```

Validation errors (400) may include field-specific details in Pydantic format.

Internal errors are logged with full traceback but return generic messages to clients for security.

## Background Jobs / Events

### Job Processing
- **Trigger**: POST to `/extract/v1` or `/batch-extract`
- **Mechanism**: 
  1. API creates job record in SQLite (status: queued)
  2. Background task enqueued via `asyncio.create_task()`
  3. Semaphore limits concurrent GPU jobs (default: 2)
  4. Job status updated to processing
  5. Extraction pipeline runs in thread pool (to avoid blocking event loop)
  6. Results stored; status updated to completed/failed
  7. Temporary attachment files cleaned up
  8. Output files retained in `blob/output/jobs/{job_id}/output/`

### Job Lifecycle
1. **Queued**: Job accepted, waiting for processing slot
2. **Processing**: Extraction actively running
3. **Completed**: Success - result available
4. **Failed**: Error - error message stored
5. **Expired**: Automatically cleaned after TTL (24 hours)

### Concurrency Control
- Maximum concurrent jobs configured via `MAX_CONCURRENT_JOBS` in `api.py` (default: 2)
- Semaphore prevents GPU oversubscription
- Each uvicorn worker has its own semaphore; global limit requires external coordination (e.g., Redis)

## How to Run Locally

### Prerequisites
- Docker Desktop or Docker Engine
- Git
- (Optional) NVIDIA GPU with drivers for GPU acceleration

### Steps
1. Clone repository:
   ```bash
   git clone <repository-url>
   cd paddleocr
   ```

2. Configure environment:
   ```bash
   cp .env.example .env   # or edit .env directly
   # Adjust variables as needed (especially OCR_DPI, LOW_DPI, etc.)
   ```

3. Build and start services:
   ```bash
   # CPU-only development
   docker compose -f docker-compose.cpu.yml up --build
   
   # GPU-accelerated (if compatible hardware available)
   docker compose -f docker-compose.yml up --build
   ```

4. Verify service is running:
   ```bash
   curl http://localhost:8000/health
   ```
   Should return 200 OK with service information.

5. Access interactive API documentation:
   - Swagger UI: http://localhost:8000/docs
   - ReDoc: http://localhost:8000/redoc

### Direct Python Execution (for development)
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .

# Run CLI directly
python -m ingestion_pipeline.main --help

# Run API server
python -m ingestion_pipeline.api
```

## How to Test

### Unit Tests
Individual test files exist in the project root:
- `test_api_transform.py` - API transformation logic
- `test_excel.py` - Excel processing
- `test_password.py` - Password-protected file handling
- `test_transform.py` / `test_transform2.py` - General transformation tests

Run specific tests:
```bash
pytest test_api_transform.py -v
```

### Integration Testing
1. Start service via Docker Compose
2. Use test scripts or manual curl requests:
   ```bash
   # Synchronous extraction
   curl -X POST "http://localhost:8000/extract" \
     -F "files=@/path/to/document.pdf" \
     -F "request_id=test-123"
   
   # Asynchronous job submission
   curl -X POST "http://localhost:8000/extract/v1" \
     -F "files=@/path/to/scan.jpg"
   
   # Check job status (repeat until completed)
   curl -X GET "http://localhost:8000/extract/v1/<job_id>"
   ```

### Test Data
Place test files in `blob/input/` directory (or any directory referenced by `INGESTION_INPUT_DIR`).

## Troubleshooting Common Issues

See [Troubleshooting Runbook](./docs/runbooks/troubleshooting.md) for detailed guidance.

### Common Problems
1. **GPU Not Detected**: Ensure NVIDIA drivers are installed and visible inside container (`nvidia-smi` works)
2. **Out of GPU Memory**: Reduce `OCR_DPI` or increase `MAX_CONCURRENT_JOBS` denominator
3. **Slow Processing**: Check if falling back to CPU due to `PADDLE_USE_GPU=false`
4. **File Not Found**: Verify `INGESTION_INPUT_DIR` points to correct directory with proper permissions
5. **Corrupted Output**: Ensure sufficient disk space in `blob/output`
6. **License Errors**: Set `PYMUPDF_PRO_KEY` if using PyMuPDF commercial features