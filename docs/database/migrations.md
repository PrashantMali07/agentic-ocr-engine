# Migrations

The PaddleOCR service currently does not have a formal migration system for its database schema. The SQLite job store is initialized with a simple table structure that has remained unchanged since the service's inception.

## Current Approach

- The database schema is defined in the `_INIT_SQL` constant in `src/ingestion_pipeline/job_store.py`
- On application start, the `JobStore._ensure_init()` method creates the table if it does not exist
- The table creation uses `CREATE TABLE IF NOT EXISTS`, which is safe to run repeatedly
- If the schema needs to change in the future, the `_INIT_SQL` would be updated, and the table would be altered accordingly

## Schema History

The job store table has always been:

```sql
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

No alterations have been made to this schema.

## Future Considerations

If the service evolves to require a more complex schema (e.g., separate tables for jobs, results, or metrics), a formal migration strategy should be adopted. Potential tools include:

- **Alembic**: Popular migration tool for SQLAlchemy (though current code doesn't use SQLAlchemy)
- **SQLite-specific tools**: Since SQLite has limited `ALTER TABLE` support, migrations often involve creating a new table and copying data
- **Custom migration scripts**: Versioned SQL scripts applied on startup

## Deployment Notes

- In containerized deployments, the `blob/output` directory is mounted as a persistent volume
- The `jobs.db` file will persist across container restarts and deployments
- When updating the service, if the schema changes, the deployment process should handle data migration
- For now, since the schema is simple and stable, no special migration steps are required during updates

## Backup and Restore

- The `jobs.db` file can be backed up by copying the file from the persistent volume
- To restore, replace the file in the volume and ensure proper permissions
- The service can continue operation with an existing jobs.db; it will read existing jobs and continue to update them