# Technology Stack

## Core Framework

| Component | Technology | Version | Purpose |
|-----------|------------|---------|---------|
| Language | Python | 3.11+ | Implementation language |
| Web Framework | FastAPI | 0.141.1 | REST API framework |
| ASGI Server | Uvicorn | 0.52.4 | Production web server |
| Data Validation | Pydantic | (via FastAPI) | Request/response modeling |
| Async Processing | asyncio | Python stdlib | Concurrency and background jobs |

## OCR and Document Processing

| Component | Technology | Version | Purpose |
|-----------|------------|---------|---------|
| OCR Engine | PaddleOCR | 2.8.0+ | Primary OCR for scanned documents |
| Document Layout | PaddleOCR[doc-parser] | 3.0+ | Advanced document structure analysis |
| Table Recognition | PaddleX | 3.0.0 | Table structure recognition |
| PDF Processing | pdfplumber | 0.11.10 | Text extraction from digital PDFs |
| PDF Processing | PyMuPDF | 1.28.2 | PDF rendering and text extraction |
| Enhanced PDF | PyMuPDF4LLM | 1.28.2 | PDF to markdown conversion |
| Layout Enhancement | PyMuPDF-layout | 1.28.2 | PDF layout analysis |
| Word Processing | python-docx | 1.2.0 | Text extraction from DOCX files |
| HTML Parsing | BeautifulSoup4 | 4.15.0 | HTML text extraction |
| HTML Parsing | lxml | 6.1.3 | Robust HTML/XML parsing |
| Spreadsheet | openpyxl | 3.1.5 | Excel file reading |
| Legacy Excel | xlrd | <2.0 | Old Excel format support |
| Encrypted Files | msoffcrypto-tool | 6.0.0 | Decrypt Office files |
| Language Detection | langdetect | 1.0.9 | Detect text language |
| Character Detection | charset-normalizer | 3.5.1 | Character encoding detection |

## Infrastructure and Dependencies

| Component | Technology | Version | Purpose |
|-----------|------------|---------|---------|
| Job Store | aiosqlite | 0.22.1 | Async SQLite wrapper for job persistence |
| Database | SQLite | Built-in | Lightweight, zero-config job storage |
| HTTP Client | httpx | 0.27.0 | Async HTTP requests (if needed) |
| CLI Utilities | tabulate | 0.10.0 | Pretty-print tabular data |
| Environment | python-dotenv | 1.2.3 | Load environment variables from .env |
| Multimedia | onnxruntime-gpu | 1.20-1.26 | GPU acceleration for ONNX models |
| GPU Driver | paddlepaddle-gpu | 3.0.0b2 | PaddlePaddle framework with CUDA support |

## Development and DevOps

| Component | Technology | Version | Purpose |
|-----------|------------|---------|---------|
| Containerization | Docker | Multi-stage build | Consistent runtime environment |
| Orchestration | Docker Compose | yaml | Local development and testing |
| Dependency Management | UV | uv_build | Fast Python package installer |
| Package Index | Custom | paddle-cu123, onnxruntime-cuda-12 | GPU-optimized package sources |
| Build System | UV | uv_build | Replacement for setuptools/pip |
| Scripting | PowerShell | .ps1 files | Azure build/deployment automation |
| Logging | Python logging | stdlib | Structured logging with request IDs |
| Log Rotation | RotatingFileHandler | stdlib | Persistent log storage |

## Quality and Testing

| Component | Technology | Version | Purpose |
|-----------|------------|---------|---------|
| Type Hints | Python 3.11+ | stdlib | Improved code quality and IDE support |
| Testing Framework | pytest | (dev dependency) | Test execution and discovery |
| Code Formatting | black | (dev dependency) | Consistent code style |
| Import Sorting | isort | (dev dependency) | Organized imports |
| Linting | flake8 | (dev dependency) | Code quality checks |
| Security | bandit | (dev dependency) | Security issue detection |
| Documentation | Sphinx | (optional) | API documentation generation |
| Diagrams | Mermaid | markdown | Architecture diagrams in docs |

## Deployment Environment

| Component | Technology | Purpose |
|-----------|------------|---------|
| Container Registry | Azure Container Registry (ACR) | Store and manage Docker images |
| Orchestration | Azure Container Apps (ACA) | Serverless container hosting |
| Compute | Azure Kubernetes Service (AKS) underlying | Managed Kubernetes infrastructure |
| Scaling | KEDA-based | Event-driven autoscaling |
| Networking | Azure Virtual Network | Secure service communication |
| Monitoring | Azure Monitor | Logging, metrics, and alerts |
| Secrets | Azure Key Vault | Secure secret management |
| Storage | Azure Blob Storage | Persistent blob storage (via mounted volumes) |

## Configuration Management

Environment variables are loaded from `.env` file and processed in `src/ingestion_pipeline/config.py`. Key categories:

1. **Paths**: Input/output directories
2. **Hardware Acceleration**: GPU/CPU selection
3. **Image Preprocessing**: DPI, enhancement, dimensions, dtype, grayscale
4. **PDF Processing**: Low/high DPI thresholds, large file handling
5. **OCR Model Configuration**: Layout detection, text detection/recognition thresholds
6. **Feature Flags**: Orientation classification, unwrapping, table recognition

## Performance Characteristics

- **GPU Acceleration**: Significant speedup for OCR processing when GPU available
- **Concurrency Control**: Semaphore limits concurrent GPU jobs to prevent OOM
- **Memory Management**: dtype configuration (uint8/float16/float32) balances precision and VRAM usage
- **Image Size Limits**: MAX_DIM prevents excessively large images that exceed model limits
- **Batch Processing**: Internal batching in PaddleOCR optimizes GPU utilization
- **Job Persistence**: SQLite store survives restarts with minimal overhead
- **Horizontal Scaling**: Multiple instances share job store via file system (single node) or can be adapted to Redis

## Security Considerations

- Input validation via Pydantic models prevents malformed requests
- File type validation restricts processed documents to supported extensions
- Temporary file isolation prevents cross-job contamination
- Secure handling of encrypted Office documents (requires password)
- No persistent storage of processed documents beyond job TTL
- API endpoints protected by deployment-level security (network policies, auth)

## Future Enhancements

- Replace SQLite job store with Redis for multi-instance deployments
- Add authentication and authorization to API endpoints
- Implement result caching for frequently processed documents
- Add metrics endpoint (Prometheus) for observability
- Implement document classification routing
- Add support for additional document formats (EPUB, etc.)
- GPU memory optimization and model quantization