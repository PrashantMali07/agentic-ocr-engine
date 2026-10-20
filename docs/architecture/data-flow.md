# Data Flow

## Synchronous Extraction Flow (`/extract`)

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI
    participant Pipeline as Extraction Pipeline
    participant Parser as Document Parser
    participant OCR as PaddleOCR Engine
    participant Store as Local File System
    
    Client->>API: POST /extract (multipart/form-data)
    API->>Pipeline: process_email_attachments()
    Pipeline->>Parser: collect_input_files()
    alt Digital File (DOCX, HTML, PDF)
        Parser->>Parser: Parse natively
        Parser-->>Pipeline: Extracted text + metadata
    else Scanned File (Image, Scanned PDF)
        Parser->>OCR: Render to image + Run OCR
        OCR-->>Parser: Layout blocks + text + confidence
        Parser-->>Pipeline: Structured blocks
    end
    Pipeline->>Store: Write JSON and Markdown outputs
    Pipeline-->>API: Transform to response format
    API-->>Client: ExtractResponse (with language detection)
```

## Asynchronous Extraction Flow (`/extract/v1`)

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI
    participant JobStore as SQLite Job Store
    participant Background as Background Task
    participant Pipeline as Extraction Pipeline
    participant Parser as Document Parser
    participant OCR as PaddleOCR Engine
    participant Store as Local File System
    
    Client->>API: POST /extract/v1 (multipart/form-data)
    API->>JobStore: Create job (queued)
    API-->>Client: JobStatusResponse (202 Accepted)
    API->>Background: Enqueue _run_extraction_job()
    Background->>JobStore: Update job (processing)
    Background->>Pipeline: process_email_attachments()
    Pipeline->>Parser: collect_input_files()
    alt Digital File
        Parser->>Parser: Parse natively
    else Scanned File
        Parser->>OCR: Render + OCR
    end
    Pipeline->>Store: Write JSON and Markdown outputs
    Pipeline->>Background: Transform to response format
    Background->>JobStore: Update job (completed/failed) + result/error
    Background-->>API: Task completion
    Client->>API: GET /extract/v1/{job_id} (polling)
    API->>JobStore: Retrieve job
    API-->>Client: JobStatusResponse (with result if completed)
```

## Validation Flow (`/validate-attachments`)

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI
    participant Pipeline as Extraction Pipeline
    participant Validator as Validation Module
    participant Store as Local File System
    
    Client->>API: POST /validate-attachments (multipart/form-data)
    API->>Pipeline: process_email_attachments()
    Pipeline->>Parser: collect_input_files()
    alt Digital File
        Parser->>Parser: Parse natively
    else Scanned File
        Parser->>OCR: Render + OCR
    end
    Pipeline->>Store: Write JSON output
    API->>Validator: validate_email()
    Validator->>Pipeline: Compare extracted vs original
    Validator-->>API: ValidationResult (per attachment)
    API-->>Client: ValidateAttachmentsResponse
```

## Key Data Structures

### Extraction Record (Internal)
```json
{
  "email_id": "request_id",
  "attachments": [
    {
      "file": "filename.pdf",
      "format": "pdf",
      "pages": 5,
      "avg_confidence": 0.92,
      "is_password_required": false
    }
  ],
  "blocks": [
    {
      "type": "text",
      "content": "Extracted text...",
      "source_file": "filename.pdf",
      "page": 1,
      "order": 0,
      "bbox": [x1, y1, x2, y2],
      "score": 0.95
    }
  ]
}
```

### API Response (ExtractResponse)
```json
{
  "request_id": "req-123",
  "documents": {
    "filename.pdf": {
      "content": "Extracted text...",
      "avg_confidence": 0.92,
      "format": "pdf",
      "pages": 5,
      "language": "en"
    }
  }
}
```

### Job Store Record
```json
{
  "job_id": "uuid-string",
  "status": "queued|processing|completed|failed",
  "created_at": "ISO timestamp",
  "updated_at": "ISO timestamp",
  "result": { /* ExtractResponse */ },
  "error": "error message if failed",
  "progress": {
    "total": 5,
    "processed": 3
  }
}
```

## File System Layout

```
blob/
├── input/                 # Default input directory (configurable)
│   └── [user-provided files]
├── output/                # Default output directory
│   ├── jobs.db            # SQLite job store
│   └── jobs/              # Job-specific directories
│       ├── {job_id}/
│       │   ├── attachments/   # Uploaded copies (cleaned after processing)
│       │   └── output/        # Extraction results
│       │       ├── [filename].json
│       │       └── [filename].md
└── logs/                  # Application logs
    └── ingestion.log
```