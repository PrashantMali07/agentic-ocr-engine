"""Tenant-scoped bounded artifact storage (Phase 01).

Artifacts (source uploads, page renders, OCR results) are referenced by
generated hex32 IDs in contracts and workflow payloads; local paths never
leave this module. Writes are atomic (temp file + rename) so a retried
activity writing the same `(job, page, stage)` key either replaces its own
artifact safely or leaves the previous one intact. Reads are bounded by
size and refuse symlink redirection.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Any

from ..processing.preflight import PreflightError

# Job/document/artifact identity: matches the existing hex32 upload layout.
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_KINDS = ("attachments", "renders", "ocr", "results")
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

def _hex32() -> str:
    return secrets.token_hex(16)


class ArtifactStore:
    """Local filesystem implementation behind the artifact-ID boundary."""

    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def from_output_root(output_dir: Path) -> "ArtifactStore":
        return ArtifactStore(Path(output_dir) / "jobs")

    # -- path handling -------------------------------------------------------

    def _job_dir(self, job_id: str) -> Path:
        if not _HEX32.fullmatch(job_id or ""):
            raise PreflightError("bad_identifier", "job/artifact IDs must be lowercase hex32")
        return self._root / job_id

    def artifact_path(self, job_id: str, kind: str, artifact_id: str, suffix: str = "") -> Path:
        # job_id first: caller-supplied identity is the traversal risk.
        if not _HEX32.fullmatch(job_id or ""):
            raise PreflightError("bad_identifier", "job/artifact IDs must be lowercase hex32")
        if kind not in _KINDS:
            raise PreflightError("bad_identifier", f"Unknown artifact kind {kind!r}")
        if not _HEX32.fullmatch(artifact_id or ""):
            raise PreflightError("bad_identifier", "Artifact IDs must be lowercase hex32")
        suffix = suffix or ""
        if suffix and not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            raise PreflightError("bad_identifier", "Artifact suffix must be a short lowercase extension")
        return self._job_dir(job_id) / kind / f"{artifact_id}{suffix}"

    # -- write/read ----------------------------------------------------------

    def put(self, job_id: str, kind: str, data: bytes, suffix: str = "") -> dict[str, Any]:
        """Atomically store bytes and return the artifact record (no path)."""
        if not data:
            raise PreflightError("empty_artifact", "An artifact must contain bytes")
        return self._store(job_id, kind, _hex32(), data, suffix)

    def put_deterministic(self, job_id: str, kind: str, key: str, data: bytes, suffix: str = "") -> dict[str, Any]:
        """Write one idempotent artifact keyed by a stable caller key.

        ``key`` is the bounded stage identity, e.g. ``<doc>:<page>:<stage>``.
        The artifact ID is derived from (job, kind, key) so a retried
        activity rewrites its own result in place instead of accumulating
        orphaned copies. The stored bytes are checksummed by the caller.
        """
        if not data:
            raise PreflightError("empty_artifact", "An artifact must contain bytes")
        if not key or len(key) > 256:
            raise PreflightError("bad_identifier", "Deterministic artifact key must be 1-256 bytes")
        artifact_id = hashlib.sha256(
            f"{job_id}:{kind}:{key}".encode("utf-8")).hexdigest()[:32]
        return self._store(job_id, kind, artifact_id, data, suffix)

    def _store(self, job_id: str, kind: str, artifact_id: str, data: bytes, suffix: str) -> dict[str, Any]:
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise PreflightError("artifact_too_large", f"Artifacts are bounded at {_MAX_ARTIFACT_BYTES} bytes")
        target = self.artifact_path(job_id, kind, artifact_id, suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".tmp-{artifact_id}"
        try:
            temp.write_bytes(data)
            os.replace(temp, target)  # atomic within the artifact directory
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise PreflightError("artifact_write_failed", "Artifact could not be stored") from exc
        return {
            "artifact_id": artifact_id,
            "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    def read_bounded(self, job_id: str, artifact_id: str, max_bytes: int = _MAX_ARTIFACT_BYTES) -> bytes:
        """Read an artifact by ID under a size bound; no symlink traversal."""
        # Extensionless artifacts (default suffix) must resolve too.
        found = [
            path
            for kind in _KINDS
            for path in (self._job_dir(job_id) / kind).glob(f"{artifact_id}*")
            if path.name == artifact_id
            or (path.name.startswith(f"{artifact_id}.") and _HEX32.fullmatch(path.stem))
        ]
        if not found:
            raise PreflightError("artifact_missing", "Artifact is not present for this job")
        if len(found) > 1:
            raise PreflightError("artifact_ambiguous", "Artifact ID collides across kinds")
        path = found[0]
        if path.is_symlink() or not path.is_file():
            raise PreflightError("artifact_unsafe", "Artifact path is unsafe")
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise PreflightError("artifact_too_large", "Artifact exceeds the read budget")
        return data

    # -- lifecycle -----------------------------------------------------------

    def cleanup_job(self, job_id: str) -> None:
        """Remove every artifact of a job (called from guarded maintenance)."""
        job_dir = self._job_dir(job_id)
        if job_dir.exists():
            for child in job_dir.iterdir():
                if child.is_dir():
                    for artifact in child.iterdir():
                        artifact.unlink(missing_ok=True)
                    child.rmdir()
            job_dir.rmdir()
        # job_dir itself invariants hold only when empty; shutil would widen scope
