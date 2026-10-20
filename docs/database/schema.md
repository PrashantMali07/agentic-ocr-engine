# Database Schema

The PaddleOCR service uses a simple SQLite database for job persistence. There is no ORM or complex schema; the job store uses a single table to store job records as JSON blobs.

## Tables

### jobs

Stores asynchronous extraction job records.

| Column | Type | Description |
|--------|------|-------------|
| `job_id` | TEXT (Primary Key) | Unique identifier for the job (UUID v4 style) |
| `data` | TEXT NOT NULL | JSON blob containing the full job status and result |
| `updated_at` | TEXT NOT NULL | Timestamp of last update (ISO 8601 format) |

## Table Definition

```sql
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

## Data Structure (JSON blob)

The `data` column contains a JSON string that conforms to the `JobStatusResponse` Pydantic model. When parsed, it has the following structure:

```json
{
  "job_id": "string",
  "status": "queued|processing|completed|failed",
  "created_at": "ISO 8601 timestamp",
  "updated_at": "ISO 8601 timestamp",
  "result": { /* ExtractResponse JSON */ } | null,
  "error": "string" | null,
  "progress": {
    "total": "integer",
    "processed": "integer"
  }
}
```

### JobStatusResponse Fields

- `job_id`: Unique job identifier
- `status`: Current job status (queued, processing, completed, failed)
- `created_at`: Timestamp when job was created
- `updated_at`: Timestamp when job was last updated
- `result`: The extraction result if job completed successfully (matches `ExtractResponse` schema), otherwise null
- `error`: Error message if job failed, otherwise null
- `progress`: 
  - `total`: Total number of files to process in this job
  - `processed`: Number of files processed so far

## Indexes

No additional indexes are created beyond the primary key on `job_id`. The table is expected to be small (jobs are expired after 24 hours), so full table scans are acceptable.

## Connection & Usage

- Database file: `blob/output/jobs.db`
- Accessed via `aiosqlite` (async SQLite wrapper) in `src/ingestion_pipeline/job_store.py`
- The `JobStore` class provides methods for create, get, update, delete, and purge_expired operations
- Jobs are automatically expired after 24 hours (configurable via `JOB_TTL` in job_store.py) by calling `purge_expired()`

## Migration Strategy

Since the schema is very simple and only contains one table, migrations are not currently implemented. If the schema needs to change in the future, the approach would be:
1. Update the `_INIT_SQL` constant in `job_store.py`
2. The table will be recreated with the new schema on next initialization (if the database file is deleted)
3. For preserving existing data, a migration script would be needed to copy data from the old table to the new schema

## Notes

- The service is designed to be stateless except for job persistence
- All processing state is either in memory (during processing) or stored in the job store
- The blob directory (`blob/output`) is intended to be a persistent volume in container deployments
- In multi-instance deployments, the SQLite database would need to be replaced with a shared store (e.g., Redis, PostgreSQL) for job sharing