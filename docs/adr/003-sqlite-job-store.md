# ADR 003: Use SQLite for Job Storage

## Status
Accepted

## Context
The PaddleOCR service needs to store state for asynchronous extraction jobs, including job status, progress, results, and errors. Initially, we considered several storage options:
- **Redis**: In-memory key-value store with persistence options, excellent for high-performance queuing
- **PostgreSQL**: Full-featured relational database with strong consistency and rich querying
- **SQLite**: Lightweight, file-based SQL database with zero configuration
- **In-memory**: Simple Python dictionaries (not persistent across restarts)

Requirements for the job store included:
- Persistence across service restarts
- Ability to store structured JSON data (job results)
- Simple querying by job ID
- Moderate write volume (job creation, updates, completion)
- Low operational overhead (preferably zero-admin)
- Suitability for containerized deployment with persistent volumes
- Potential for horizontal scaling in future

## Decision
We chose SQLite as the job storage mechanism for the following reasons:
1. **Simplicity and Zero Administration**: SQLite requires no separate service to run, manage, or monitor. It's just a file in the persistent volume.
2. **Sufficient Performance**: For our expected workload (hundreds of jobs per day, not thousands per second), SQLite's performance is more than adequate.
3. **ACID Transactions**: Despite being lightweight, SQLite provides full ACID guarantees, ensuring job state consistency.
4. **JSON Storage**: We store each job as a JSON blob in a single table, which SQLite handles efficiently for our read/write patterns.
5. **Operational Simplicity**: Backup and restore is as simple as copying the file. No complex replication or clustering setup needed.
6. **Container-Friendly**: Works well with persistent volume mounts in Kubernetes, Azure Container Apps, or Docker Compose.
7. **Familiarity and Reliability**: Widely used, well-understood, and highly reliable for embedded/storage use cases.
8. **Future Migration Path**: If scaling requirements change, migrating to PostgreSQL or Redis would be straightforward due to the simple schema.

We rejected Redis because:
- It requires managing a separate service (though simple, it's another moving part)
- Persistence configuration adds complexity (RDB vs AOF)
- Overkill for our current throughput needs
- Would introduce another dependency and potential failure point

We rejected PostgreSQL because:
- Significantly more complex to set up and manage
- Requires connection pooling and more careful configuration
- Over-provisioned for our simple key-value-with-blob usage
- Higher operational burden for minimal gain at current scale

## Consequences
### Positive
- Minimal operational overhead: no separate database service to monitor or patch
- Automatic backup/restore with the persistent volume
- Predictable performance characteristics for our workload
- Simplified local development: no need to run Redis/PostgreSQL locally
- Easy to inspect job data with SQLite CLI tools if needed
- Single file makes versioning and snapshotting trivial

### Negative
- Limited horizontal scalability: Multiple instances cannot safely write to the same SQLite file without network filesystem risks
- Write performance degrades with very high concurrency (though we limit concurrent jobs via semaphore)
- Database file can grow over time (mitigated by TTL-based cleanup)
- Lack of advanced querying capabilities (we don't need complex queries anyway)
- Single point of failure if the persistent volume is lost (mitigated by volume backups)

## Mitigations for Limitations
1. **Horizontal Scaling**: If needed in future, we can:
   - Replace SQLite with Redis or PostgreSQL while keeping the same interface
   - Use a shared network filesystem with appropriate locking (less ideal)
   - Implement job sharding across multiple stores
2. **Database Growth**: The `JobStore.purge_expired()` method removes jobs older than TTL (24 hours by default). This can be called periodically.
3. **Backup Strategy**: Regular snapshots of the persistent volume containing `blob/output/jobs.db`
4. **Monitoring**: Track job store size and old job count as health metrics

## Related Documents
- [Job Store Implementation](./src/ingestion_pipeline/job_store.py)
- [Database Schema](./docs/database/schema.md)
- [Runbooks: Local Setup](./docs/runbooks/local-setup.md)
- [Runbooks: Deployment](./docs/runbooks/deployment.md)