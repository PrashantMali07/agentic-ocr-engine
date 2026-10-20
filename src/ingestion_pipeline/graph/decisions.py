"""Bounded, stateless LangGraph decisions used by the Temporal lane.

Only page identity, classification and audit metadata enter this graph. OCR
blocks and images remain in job-scoped artifacts, outside workflow history.
"""

from __future__ import annotations

import math
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class DecisionState(TypedDict, total=False):
    classification: Literal["digital", "scanned"]
    extension: str
    page_status: str
    requires_hitl: bool
    audit_valid: bool
    confidence: float | None
    action: Literal["digital", "native", "gpu", "review", "accept", "fail"]
    reason: str


def _route(state: DecisionState) -> DecisionState:
    extension = state["extension"]
    if extension in {"docx", "html", "htm", "xlsx", "xls", "csv"}:
        return {"action": "native", "reason": "native_document"}
    if extension == "pdf" and state["classification"] == "digital":
        return {"action": "digital", "reason": "text_layer"}
    return {"action": "gpu", "reason": "image_or_scanned_page"}


def _audit(state: DecisionState) -> DecisionState:
    if state["page_status"] == "failed":
        return {"action": "fail", "reason": "page_failed"}
    if state["page_status"] == "completed" and state.get("confidence") is None:
        return {"action": "review", "reason": "confidence_unknown"}
    if state["page_status"] == "needs_review" or state.get("requires_hitl") or not state.get("audit_valid", False):
        return {"action": "review", "reason": "audit_flagged"}
    confidence = state.get("confidence")
    if confidence is not None and confidence < 0.85:
        return {"action": "review", "reason": "low_confidence"}
    return {"action": "accept", "reason": "audit_passed"}


def _compile(node):
    builder = StateGraph(DecisionState)
    builder.add_node("decide", node)
    builder.add_edge(START, "decide")
    builder.add_edge("decide", END)
    return builder.compile()


_ROUTE_GRAPH = _compile(_route)
_AUDIT_GRAPH = _compile(_audit)


def route_page(classification: str, extension: str) -> dict[str, str]:
    if classification not in {"digital", "scanned"}:
        raise ValueError("Unsupported page classification")
    if extension not in {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "docx", "html", "htm", "xlsx", "xls", "csv"}:
        raise ValueError("Unsupported page extension")
    result = _ROUTE_GRAPH.invoke({"classification": classification, "extension": extension})
    return {"action": result["action"], "reason": result["reason"]}


def audit_page(status: str, audit: dict, confidence: float | None) -> dict[str, str]:
    if status not in {"completed", "empty", "failed", "needs_review"}:
        raise ValueError("Unsupported page status")
    if confidence is not None and (not isinstance(confidence, (int, float)) or
                                   not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("Invalid measured confidence")
    result = _AUDIT_GRAPH.invoke({
        "page_status": status, "requires_hitl": bool(audit.get("requires_hitl")),
        "audit_valid": bool(audit.get("is_valid")), "confidence": confidence,
    })
    return {"action": result["action"], "reason": result["reason"]}
