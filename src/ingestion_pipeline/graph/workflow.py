"""
LangGraph Workflow Builder & Compiler
-------------------------------------
Assembles the DocumentStateGraph with:
- Conditional routing: Digital PDF vs Scanned Pipeline
- Pure PaddleOCR + LayoutReader (CPU)
- OpenCV morphological table reconstruction
- Hybrid fallback to Pytesseract
- Native Human-in-the-Loop interrupt()
"""

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Union

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    auditor_node,
    fallback_tesseract_node,
    finalize_node,
    hitl_gate_node,
    ocr_node,
    preprocess_node,
    reading_order_node,
    table_structuring_node,
    triage_node,
)
from .state import DocumentGraphState

log = logging.getLogger("ingest.graph.workflow")


def route_after_triage(state: DocumentGraphState) -> str:
    """If document is a pure digital PDF, route directly to finalize; otherwise preprocess image."""
    if state.get("is_digital", False):
        return "finalize"
    return "preprocess"


def route_after_auditor(state: DocumentGraphState) -> str:
    """If audit detects low confidence or critical issues and fallback not yet run, trigger Pytesseract."""
    audit = state.get("audit", {})
    already_fallback = state.get("fallback_triggered", False)

    overall_conf = audit.get("overall_confidence", 1.0)
    critical_issues = audit.get("critical_issues_count", 0)

    # Condition for hybrid fallback: low confidence or critical issues
    if (overall_conf < 0.85 or critical_issues > 0) and not already_fallback:
        log.info("Auditor routed to Hybrid Pytesseract Fallback (conf=%.2f, critical=%d)", overall_conf, critical_issues)
        return "fallback_tesseract"

    return "hitl_gate"


def build_document_ingestion_graph(checkpointer: Optional[Any] = None) -> Any:
    """Build and compile the LangGraph document ingestion workflow."""
    builder = StateGraph(DocumentGraphState)

    # 1. Add all nodes
    builder.add_node("triage", triage_node)
    builder.add_node("preprocess", preprocess_node)
    builder.add_node("ocr", ocr_node)
    builder.add_node("reading_order", reading_order_node)
    builder.add_node("table_structuring", table_structuring_node)
    builder.add_node("auditor", auditor_node)
    builder.add_node("fallback_tesseract", fallback_tesseract_node)
    builder.add_node("hitl_gate", hitl_gate_node)
    builder.add_node("finalize", finalize_node)

    # 2. Add edges & conditional routing
    builder.add_edge(START, "triage")

    builder.add_conditional_edges(
        "triage",
        route_after_triage,
        {
            "finalize": "finalize",
            "preprocess": "preprocess",
        }
    )

    builder.add_edge("preprocess", "ocr")
    builder.add_edge("ocr", "reading_order")
    builder.add_edge("reading_order", "table_structuring")
    builder.add_edge("table_structuring", "auditor")

    builder.add_conditional_edges(
        "auditor",
        route_after_auditor,
        {
            "fallback_tesseract": "fallback_tesseract",
            "hitl_gate": "hitl_gate",
        }
    )

    builder.add_edge("fallback_tesseract", "hitl_gate")
    builder.add_edge("hitl_gate", "finalize")
    builder.add_edge("finalize", END)

    # 3. Compile graph with checkpointer for state persistence
    saver = checkpointer if checkpointer is not None else MemorySaver()
    app = builder.compile(checkpointer=saver)
    return app


class ReviewRequired(RuntimeError):
    """Graph extraction stopped before a final result because review is required."""


def get_document_graph() -> Any:
    """Return a fresh development graph with an isolated in-memory checkpoint."""
    return build_document_ingestion_graph()


def run_document_graph(file_path: Union[str, Path], thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute document extraction through the LangGraph workflow.

    Args:
        file_path: Path to the input PDF or image file.
        thread_id: Unique thread identifier for state tracking & checkpointer.

    Returns:
        Final extraction payload including clean text, tables, and audit report.
    """
    # The compatibility runner cannot resume a graph review. Keep its checkpoint
    # local to this call and delete it even when extraction fails or interrupts.
    graph = get_document_graph()
    t_id = thread_id or uuid.uuid4().hex

    config = {"configurable": {"thread_id": t_id}}
    initial_state = {"file_path": str(file_path)}

    try:
        output_state = graph.invoke(initial_state, config=config)
        if output_state.get("__interrupt__") or (
            output_state.get("hitl_required") and not output_state.get("human_corrections")
        ):
            raise ReviewRequired("Document requires human review; execution did not complete")
        result = output_state.get("final_result")
        if not result:
            raise RuntimeError("Document extraction did not produce a final result")
        return result
    finally:
        checkpointer = getattr(graph, "checkpointer", None)
        if checkpointer is not None:
            checkpointer.delete_thread(t_id)
