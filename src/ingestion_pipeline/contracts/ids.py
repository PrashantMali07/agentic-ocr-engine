"""Identifier formats shared by V1 ingestion contracts.

Contracts deliberately avoid importing application internals so the same
module can cross process boundaries without loading GPU/OCR dependencies.
"""

from __future__ import annotations

import re

# Server-generated job identifier. Never derived from a user filename or a
# local path, never logged together with document content.
JOB_ID = re.compile(r"^[0-9a-z][0-9a-z-]{0,79}$")

# 32-hex document/artifact identifiers, matching the storage layout used by
# the existing upload pipeline (already hex32 in services/job_runner.py).
HEX32_ID = re.compile(r"^[0-9a-f]{32}$")

# Full-content SHA-256 checksums recorded in artifacts and result payloads.
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Bounded identifiers: artifact IDs in decision evidence, field references in
# provenance records, and idempotency keys. Prefer HEX32_ID where one applies.
BOUNDED_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")

# Policy versions tag the exact routing/audit rules a decision used, so a
# replay can reproduce it or flag a versioned change instead of drifting.
POLICY_VERSION = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

# Display names are user-visible metadata only. They must not carry local
# absolute paths, drive artifacts, or become workflow IDs.
DISPLAY_NAME = re.compile(r"^[^/\\<>]{1,255}$")

def is_job_id(value: str) -> bool:
    return bool(JOB_ID.fullmatch(value))

def is_hex32_id(value: str) -> bool:
    return bool(HEX32_ID.fullmatch(value))

def is_sha256(value: str) -> bool:
    return bool(SHA256.fullmatch(value))

def is_policy_version(value: str) -> bool:
    return bool(POLICY_VERSION.fullmatch(value))

def is_display_name(value: str) -> bool:
    return bool(DISPLAY_NAME.fullmatch(value))
