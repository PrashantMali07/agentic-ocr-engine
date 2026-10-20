# Phase 02 design rationale

Temporal should own durable coordination because retry, cancellation, and human waiting must survive API and worker restarts. The current SQLite queue already does this for the supported single-host route, so replacing it mid-development would mix a migration with a new architecture. An isolated scheduler marker keeps a working comparator and prevents double execution.

The admission-to-workflow gap is the critical consistency hazard: database commit can succeed while workflow start fails, or workflow start can succeed before the API records it. A persisted intent plus deterministic workflow ID and reconciliation handles both crash orders. Exactly-once execution of native activity code is not assumed; idempotent artifact and result commits provide one logical result.

The workflow history is durable and replayed. Putting OCR transcripts, images, or large tables in activity payloads increases history size, privacy exposure, and replay cost. Artifact references and small checksummed outcomes are the proper boundary. Worker concurrency settings alone are local to a process; a deliberate single GPU process (or later distributed resource lease) is needed for the first capacity claim.

Human review is a lifecycle transition, not a graph checkpoint. FastAPI authenticates the reviewer and stores the correction; Temporal waits and resumes. This avoids two different review states that can disagree after a restart.
