# Local Setup Guide

This guide explains how to set up and run the PaddleOCR service locally for development and testing.

## Prerequisites

- [Git](https://git-scm.com/)
- [Docker Engine](https://www.docker.com/products/docker-desktop) or [Docker Desktop](https://www.docker.com/products/docker-desktop)
- (Optional) [NVIDIA GPU](https://www.nvidia.com/Download/index.aspx) with drivers for GPU acceleration
- (Optional) [Python 3.11+](https://www.python.org/downloads/) for direct execution (not required if using Docker)

## Step 1: Clone the Repository

```bash
git clone <repository-url>
cd paddleocr
```

## Step 2: Configure Environment

Copy the example environment file and adjust as needed:

```bash
cp .env.example .env
# Or if .env doesn't exist, create one:
# cp .env .env   # if .env.example is not present, but we have .env already

# Edit .env to set any non-default values
# Key variables to consider:
# - OCR_DPI: DPI for PDF rendering (default 100)
# - PADDLE_USE_GPU: Set to "false" if no GPU is available
# - INGESTION_INPUT_DIR: Default "blob/input"
# - INGESTION_OUTPUT_DIR: Default "blob/output"
```

> **Note**: The repository already includes a `.env` file with default values. You can use it as-is for initial testing.

## Step 3: Build and Run with Docker Compose

### Option A: CPU-Only (Recommended for most development)

```bash
docker compose -f docker-compose.cpu.yml up --build
```

### Option B: GPU-Accelerated (if you have NVIDIA GPU and drivers)

```bash
docker compose -f docker-compose.yml up --build
```

> The first build may take several minutes as it downloads base images and installs Python dependencies.

## Step 4: Verify the Service is Running

Once the containers are up, check the health endpoint:

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "version": "0.2.0",
  "allowed_file_extension": [
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".docx",
    ".html",
    ".htm",
    ".xls",
    ".xlsx"
  ],
  "endpoints": {
    "health": {
      "path": "/health",
      "methods": [
        "GET"
      ]
    },
    "extract": {
      "path": "/extract",
      "methods": [
        "POST"
      ]
    },
    // ... other endpoints
  },
  "description": "Multi-extension data extraction pipeline using PaddleOCR"
}
```

## Step 5: Test the API

### Synchronous Extraction

```bash
curl -X POST "http://localhost:8000/extract" \
  -F "files=@/path/to/test/document.pdf" \
  -F "request_id=test-123"
```

### Asynchronous Job Submission

```bash
# Submit job
JOB_ID=$(curl -s -X POST "http://localhost:8000/extract/v1" \
  -F "files=@/path/to/test/scan.jpg" | jq -r .job_id)

# Poll for completion
while true; do
  STATUS=$(curl -s "http://localhost:8000/extract/v1/$JOB_ID" | jq -r .status)
  echo "Job status: $STATUS"
  if [[ "$STATUS" == "completed" || "$STATUS" == "failed" ]]; then
    break
  fi
  sleep 2
done

# Get final result
curl -s "http://localhost:8000/extract/v1/$JOB_ID"
```

## Step 6: Access Interactive Documentation

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Step 7: Run CLI Directly (Optional)

If you prefer to run the CLI without Docker:

```bash
# Create and activate virtual environment (optional but recommended)
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .

# Run CLI help
python -m ingestion_pipeline.main --help

# Process an email (requires input files in blob/input)
python -m ingestion_pipeline.main --email-id test-email --attachments /path/to/file.pdf
```

## Step 8: Stopping the Service

To stop the Docker Compose setup:

```bash
# In the same directory where you ran `docker compose up`
docker compose down
```

To also remove containers, networks, and volumes:

```bash
docker compose down -v
```

## Troubleshooting Local Setup

See [Troubleshooting Guide](./troubleshooting.md) for common issues.

### Common Problems
1. **Port Already in Use**: Change the port in docker-compose.yml or stop the conflicting service
2. **GPU Not Detected**: Ensure NVIDIA Container Toolkit is installed and nvidia-smi works inside container
3. **Slow First Build**: Be patient; subsequent starts are faster due to cached layers
4. **Permission Errors**: Ensure the blob directory is readable/writable by the container user
5. **Out of Memory**: Reduce OCR_DPI or increase swap space

## Next Steps

- Explore the API with the interactive documentation
- Try processing different document types (PDF, images, Office files)
- Check the logs: `docker compose logs -f`
- Modify code and rebuild: `docker compose up --build`