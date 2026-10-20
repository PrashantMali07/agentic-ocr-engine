# Multi-stage build for Azure Container Apps
# Stage 1: Build dependencies
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS builder

# Install system dependencies for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install UV for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv

# CPU image uses a fully resolved, hashed dependency set.
COPY requirements-cpu.lock ./
RUN uv venv /app/.venv \
  && uv pip sync --python /app/.venv/bin/python --require-hashes requirements-cpu.lock

COPY scripts/prefetch_models.py /tmp/prefetch_models.py
COPY src/ingestion_pipeline/model_artifacts.py /tmp/model_artifacts.py
COPY model-artifacts.json /app/model-artifacts.json
RUN /app/.venv/bin/python /tmp/prefetch_models.py \
  && /app/.venv/bin/python /tmp/model_artifacts.py verify --root /root/.paddlex/official_models --manifest /app/model-artifacts.json

# Stage 2: Runtime
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS runtime

# Install runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # For PDF processing
    poppler-utils \
    # For image processing (libgl1 replaces obsolete libgl1-mesa-glx on Debian trixie+)
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    # For OCR
    tesseract-ocr \
    libtesseract-dev \
    # Cleanup
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /root/.paddlex/official_models /home/appuser/.paddlex/official_models
COPY --from=builder /app/model-artifacts.json /app/model-artifacts.json

# Copy application code
COPY src/ ./src/
COPY pyproject.toml ./

# Create directories for temp files and output
RUN mkdir -p /tmp/ingestion /app/output && \
    chown -R appuser:appuser /app/output /tmp/ingestion && \
    chown appuser:appuser /home/appuser && \
    chmod -R a-w /home/appuser/.paddlex/official_models && \
    chmod 700 /app/output /tmp/ingestion

# Switch to non-root user
USER appuser

# Set Python path
ENV PYTHONPATH=/app/src
ENV PATH="/app/.venv/bin:$PATH"
ENV HOME=/home/appuser
ENV OCR_MODEL_ROOT=/home/appuser/.paddlex/official_models
ENV OCR_MODEL_MANIFEST=/app/model-artifacts.json

# Environment variables for Azure Container Apps
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV INGESTION_OUTPUT_DIR=/app/output
ENV INGESTION_INPUT_DIR=/app/output/input

# PaddlePaddle configuration
ENV PADDLE_USE_GPU=false
ENV USE_TENSORRT=false
ENV USE_ONNX=false

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=5).raise_for_status()" || exit 1

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "-m", "ingestion_pipeline.api"]
