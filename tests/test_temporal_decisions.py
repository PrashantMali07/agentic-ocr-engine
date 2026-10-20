"""Bounded graph actions cannot silently accept missing or failed evidence."""

import math

import pytest

from ingestion_pipeline.graph.decisions import audit_page, route_page


@pytest.mark.parametrize(("classification", "extension", "action"), [
    ("digital", "pdf", "digital"),
    ("scanned", "pdf", "gpu"),
    ("scanned", "webp", "gpu"),
    ("digital", "docx", "native"),
])
def test_route_is_bounded(classification, extension, action):
    assert route_page(classification, extension)["action"] == action


def test_audit_requires_review_for_unknown_or_low_confidence():
    clean = {"is_valid": True, "requires_hitl": False}
    assert audit_page("completed", clean, None)["action"] == "review"
    assert audit_page("completed", clean, 0.5)["action"] == "review"
    assert audit_page("completed", clean, 0.95)["action"] == "accept"
    assert audit_page("completed", {"is_valid": False}, 0.95)["action"] == "review"
    assert audit_page("failed", clean, None)["action"] == "fail"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -0.1, 1.1, "0.95"])
def test_invalid_confidence_cannot_be_accepted(bad):
    with pytest.raises(ValueError):
        audit_page("completed", {"is_valid": True}, bad)
