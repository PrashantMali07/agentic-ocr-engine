"""Dormant engines cannot bypass production's disabled network delivery."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from ingestion_pipeline.extraction import layout_order
from ingestion_pipeline.temporal.activities import dispatch_webhook_activity
from ingestion_pipeline.temporal.client import start_ingestion_workflow


def test_temporal_callback_is_rejected_before_network():
    # The workflow derives identity from the server-generated job ID; a
    # callback destination in the request is rejected before any connection.
    job_request = {
        "job_id": "5" * 32,
        "documents": [],
        "callback_url": "http://127.0.0.1/",
    }
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(start_ingestion_workflow(job_request))
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(dispatch_webhook_activity({"callback_url": "http://127.0.0.1/"}))


def test_layoutreader_requires_explicit_local_directory(tmp_path, monkeypatch):
    layout_order.get_layoutreader_model.cache_clear()
    monkeypatch.delenv("LAYOUTREADER_MODEL_DIR", raising=False)
    with pytest.raises(RuntimeError, match="local model"):
        layout_order.get_layoutreader_model()

    selected = []
    model = SimpleNamespace(eval=lambda: None)
    loader = SimpleNamespace(from_pretrained=lambda *args, **kwargs: (
        selected.append((args, kwargs)) or model
    ))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(LayoutLMv3ForTokenClassification=loader))
    monkeypatch.setenv("LAYOUTREADER_MODEL_DIR", str(tmp_path))
    layout_order.get_layoutreader_model.cache_clear()
    try:
        assert layout_order.get_layoutreader_model() is model
        assert selected == [((str(tmp_path),), {"local_files_only": True})]
    finally:
        layout_order.get_layoutreader_model.cache_clear()
