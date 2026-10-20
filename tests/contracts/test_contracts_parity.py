"""Parity test: contract JobStateV1 vs the legacy API JobStatus enum.

The compatibility map (contracts/README.md) promises the two enums stay
one-to-one so API polling keeps a single lifecycle truth. Drift here must
fail CI, not surface at runtime in Phase 02. This module imports the
application schema, pulling the heavier dependency chain the other contract
tests intentionally avoid.
"""

from ingestion_pipeline.contracts import JobStateV1
from ingestion_pipeline.schema.responses import JobStatus


def test_job_state_v1_matches_legacy_status_values_exactly():
    assert {state.value for state in JobStateV1} == {status.value for status in JobStatus}
    for member in JobStateV1:
        assert member.value == JobStatus[member.name].value
