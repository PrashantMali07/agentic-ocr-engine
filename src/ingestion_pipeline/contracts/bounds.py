"""Payload-size guards for contract metadata crossing process boundaries.

Temporal payloads must stay metadata-sized: artifact IDs, hashes, statuses,
counts, and reason codes. Document bytes, rendered page images, full OCR
transcripts, table payloads, and raw VLM responses belong in tenant-scoped
artifact storage and are referenced by ID instead.

The limit is intentionally conservative: one full workflow state's decision
metadata, not a page payload. Tests assert every contract example serializes
under this budget, so an accidental unbounded field (`content`, token lists,
image arrays) fails in Phase 00 rather than surfacing as a Temporal replay
failure in Phase 02.
"""

from __future__ import annotations

from pydantic import BaseModel

DEFAULT_METADATA_BUDGET_BYTES = 65_536

class MetadataBudgetError(ValueError):
    """A contract payload exceeds the metadata budget for workflow history."""

def assert_metadata_budget(
    model: BaseModel,
    limit_bytes: int = DEFAULT_METADATA_BUDGET_BYTES,
) -> None:
    """Serialize a contract model to compact JSON and enforce the budget."""
    payload = model.model_dump_json(exclude_none=True)
    size = len(payload.encode("utf-8"))
    if size > limit_bytes:
        raise MetadataBudgetError(
            f"{type(model).__name__} metadata is {size} bytes; "
            f"workflow payloads must stay within {limit_bytes} bytes "
            "(bounds.py documents the rule; document bytes belong in artifacts)"
        )
