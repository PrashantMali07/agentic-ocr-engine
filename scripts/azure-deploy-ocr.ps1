# The former script used ACR admin credentials and deployed a single container
# with no proven durable SQLite/worker topology. Keep it fail-closed until a
# reviewed single-host or distributed architecture and release gates exist.
$ErrorActionPreference = "Stop"
throw "Azure deployment is disabled. Verify the GPU image, shared durable API/worker storage, managed registry identity, encrypted backups, alerting, and Phase 6 acceptance before adding a new deployment workflow. See docs/runbooks/deployment.md."
