# Production operations implementation plan

Status: **planned, not implemented** (19 October 2026). This plan is for the supported FastAPI `/extract/v1` + worker + SQLite legacy OCR path. It does not authorize a deployment or enable Temporal, LangGraph, callbacks, or the disabled Azure deployment script.

## Decisions and owners before implementation

| Decision | Owner | Record before work starts |
| --- | --- | --- |
| Runtime and storage topology | Platform owner | One host with a durable local POSIX filesystem shared by API and worker, or a funded replacement for SQLite with tested distributed job ownership. Do not treat an Azure Blob mount or separate container filesystems as a SQLite database volume. |
| Recovery objectives | Business/data owner | Recovery point (maximum lost work), recovery time, retention, legal hold, and whether queued inputs/review artifacts must survive a host failure. |
| Encryption and key custody | Security/platform owner | Backup destination, key-management service, encryption mode, rotation, authorized restore identities, and audit-log destination. |
| Alert routing | Operations owner | On-call destination, escalation path, maintenance windows, and who acknowledges incidents. |
| Ingress | Network/security owner | Private network boundary, TLS termination, certificate ownership, caller identity, allowed origins, and whether a trusted gateway additionally authenticates callers. |

There is no safe production default for these choices. The code's `ENCRYPTED_BACKUPS` flag is a policy assertion only; setting it to `true` does not create or encrypt a backup.

## 1. Prove the hosting topology

1. Draw the exact API, worker, SQLite/WAL, input/output, model, monitoring, and backup data paths. Confirm the API and worker see the **same** `INGESTION_OUTPUT_DIR` and `jobs.db` on one host, with one active OCR child. Use a durable volume with adequate free space and predictable filesystem semantics. If the intended platform cannot provide that, stop and plan a database/queue migration first.
2. Set service identities, volume ownership, resource limits, restart policy, and a private network. Keep `ENABLE_SYNC_EXTRACTION=false` and `ENABLE_LANGGRAPH_ENGINE=false`; do not start the dormant Temporal worker. Keep model files read-only and verify `model-artifacts.json` on startup.
3. Test host/container restart with a queued job, a processing job, and a review-pending job. Verify job ownership, retry limits, tenant isolation, and retention after recovery. Test schema migration and rollback on a copy containing legacy rows.

**Evidence:** topology diagram, mount/identity configuration, restart and migration test log. **Pass condition:** no lost or duplicate terminal result, no cross-tenant read, and no dependence on a second host seeing SQLite over an unapproved mount.

## 2. Encrypted backup and restore

1. Inventory the data that must recover together: `jobs.db` (including review and observation rows), queued input files, retained result/artifact files, and the configuration needed to read them. A database-only restore can leave queued jobs pointing to missing files. Choose either a coordinated quiesced snapshot of database plus artifacts or a documented reconciliation procedure that safely fails/requeues inconsistent jobs.
2. For a live SQLite copy, use the SQLite online backup API (Python's `sqlite3.Connection.backup`) or a proven transactionally consistent snapshot. Do not simply copy a live `jobs.db` file while WAL writes are active. Validate the copied database with `PRAGMA integrity_check` before packaging. [SQLite's backup documentation](https://www.sqlite.org/backup.html) and [Python's sqlite3 backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup) describe the supported live-copy approach.
3. Encrypt **before** the backup leaves the trusted host using an approved authenticated-encryption/KMS design. Keep encryption keys out of the image, repository, job database, and backup destination. Restrict backup read, write, delete, decrypt, and restore permissions separately; enable access/audit logs. Define key rotation and failure behavior when KMS or backup storage is unavailable.
4. Set backup cadence from the approved recovery point. Make retention explicit for both backups and copied OCR artifacts. Live job deletion after 24 hours, or seven days for review-pending jobs, does not delete historical backups. The existing retention runbook calls for backup snapshots containing job data to expire within 24 hours unless a longer period is expressly approved. Apply the approved backup expiry and test that expired copies really disappear.
5. Perform a restore **in an isolated staging location**: retrieve an encrypted copy, decrypt with the intended restore identity, validate checksums and SQLite integrity, start API and worker with restored artifacts, verify representative jobs and review history, then run retention cleanup. Measure recovery time and log any unrecoverable queued work.
6. Only after the scheduled job, encryption, permissions, expiry, and restore drill pass, set `ENCRYPTED_BACKUPS=true` in the production environment. Alert on missed backups, failed encryption/upload, expiry failure, and failed restore verification.

**Evidence:** backup manifest and checksum, encryption/key-policy evidence without secrets, integrity-check output, timed restore drill, expiry test, and owner sign-off. **Pass condition:** an authorized operator can restore coherent data within the approved objectives; an unauthorized identity cannot read or decrypt the backup.

## 3. External monitoring and alerts

1. Poll public `/health` for liveness, private `/ready` for database/worker/disk readiness, and admin-authenticated `/operations/metrics` for queue age, retries, worker observations, free disk, cleanup errors, and active alert labels. Protect the admin credential in the monitor's secret store; do not expose the metrics endpoint to the public internet.
2. Route the existing labels `worker_stale`, `queue_stuck`, `processing_stuck`, `review_overdue`, `queue_capacity`, `disk_low`, and `artifact_cleanup_failed` to the on-call system. The current code activates them at the thresholds in [retention and monitoring](docs/runbooks/retention-and-monitoring.md). Add external alerts for missing scrape/monitor health, failed backup, and repeated HTTP 5xx. Decide warning/critical severity and deduplication by environment and host.
3. In staging, exercise one alert at a time (for example stop the worker long enough to lose heartbeat, create a controlled stuck queue, or simulate backup failure). Confirm the alert reaches the intended person, carries no document text/API key, resolves when healthy, and produces an incident/audit record. Do not fill production storage or stop a production worker merely to test a rule.
4. Keep dashboards for arrival rate, accepted/rejected submissions, queue depth/oldest age, p95/p99 end-to-end time, success/partial/review/failure counts, retries, CPU RSS, GPU VRAM, scratch usage, and cleanup. Supplement process samples with host/container metrics because the in-app peaks do not represent whole-device or whole-host maxima.

**Evidence:** alert rules, dashboard, synthetic alert delivery/acknowledgement log, and redaction review. **Pass condition:** a real operator receives and acknowledges each critical test alert, including loss of the monitor itself.

## 4. Private authenticated HTTPS ingress

1. Place the API behind a private ingress reachable only from approved callers. Terminate TLS with managed certificates; test renewal, hostname validation, modern protocol policy, and rejection of plain HTTP or unapproved public paths. Use network policy/firewall rules to restrict inbound traffic and outbound access to only required services.
2. Supply `INGESTION_API_KEY_SHA256_JSON` from a secret manager. Give each caller a separate tenant/subject and minimum roles; use distinct `extract`, `review`, and `admin` credentials. Keep original keys out of logs, images, shell history, and tickets. Define rotation and revocation.
3. Test authenticated upload and polling through the actual ingress. Also test missing/invalid keys, a tenant reading another tenant's job, a non-reviewer changing a review, stale review versions, oversized uploads, and disabled routes. Verify error responses and gateway logs contain no OCR text or credentials.
4. Confirm `/health` and `/ready` exposure matches policy, while `/operations/metrics` is admin-only. Verify ingress timeouts allow upload/poll behavior without turning the route into synchronous OCR processing.

**Evidence:** ingress/network policy, TLS test, identity/role mapping, negative authorization tests, and key-rotation drill. **Pass condition:** only authorized private callers can submit/read their tenant's jobs; admin telemetry and document content stay private.

## Final release handoff

The platform, security, business, and operations owners review the four evidence sets and [GPU/quality evidence](GPU_LOAD_TEST.MD) together. Preserve the exact image digest, configuration version, backup key reference, and rollback image/database compatibility. Re-enable the Azure deployment script only after a new implementation enforces this topology and these gates. The current script deliberately throws instead of deploying.
