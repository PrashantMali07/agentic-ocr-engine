"""Tests for the VLM provider layer (ollama / openai / gemini).

No network access: providers accept an injectable httpx transport, and the
OpenAI SDK client is stubbed. These tests pin the runner contract: bounded
inputs and one JSON object out.
"""

import dataclasses
import json

import httpx
import pytest

from ingestion_pipeline.vlm import (
    MAX_IMAGE_BYTES,
    ProviderError,
    VlmConfig,
    extract_json_object,
    load_bounded_image,
)
from ingestion_pipeline.vlm.providers import call_gemini, call_ollama, call_openai, get_provider

PAGE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000153f9507f0000000049454e44ae426082"
)


def _config(**overrides):
    """Bounded test config; transport injection keeps calls off the network."""
    base = VlmConfig(
        provider="ollama", model="gemma4:31b-cloud", base_url="http://x", api_key=None,
        timeout_seconds=2, max_output_bytes=2_000_000, max_image_bytes=MAX_IMAGE_BYTES,
    )
    return dataclasses.replace(base, **overrides)


# --- config -----------------------------------------------------------------

def test_default_config_is_ollama_with_gemma_cloud_model(monkeypatch):
    for var in ("VLM_PROVIDER", "VLM_MODEL", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    config = VlmConfig.from_env()
    assert config.provider == "ollama"
    assert config.model == "gemma4:31b-cloud"
    assert config.is_hosted is True  # -cloud routes through Ollama's remote service


def test_config_rejects_unknown_provider(monkeypatch):
    with pytest.raises(ValueError):
        VlmConfig.from_env(provider="huggingface")


def test_config_requires_api_keys_for_hosted_providers(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        VlmConfig.from_env(provider="openai")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        VlmConfig.from_env(provider="gemini")


# --- image guards -----------------------------------------------------------

def test_load_bounded_image_rejects_missing_or_symlink(tmp_path):
    with pytest.raises(ProviderError, match="not found or unsafe"):
        load_bounded_image(tmp_path / "nope.png")
    real = tmp_path / "real.png"
    real.write_bytes(PAGE_PNG)
    link = tmp_path / "link.png"
    link.symlink_to(real)
    with pytest.raises(ProviderError, match="not found or unsafe"):
        load_bounded_image(link)


def test_load_bounded_image_rejects_unknown_or_oversize(tmp_path):
    csv = tmp_path / "page.csv"
    csv.write_text("a,b")
    with pytest.raises(ProviderError, match="Unsupported page image type"):
        load_bounded_image(csv)
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(ProviderError, match="exceeds"):
        load_bounded_image(big)


# --- ollama -----------------------------------------------------------------

def _ollama_transport(content: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["model"]
        assert len(body["messages"][0]["images"]) == 1
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    return httpx.MockTransport(handler)


def test_ollama_call_parses_fenced_json(tmp_path):
    image = tmp_path / "page-0001.png"
    image.write_bytes(PAGE_PNG)
    config = _config(
        transport=_ollama_transport('```json\n{"transcript": "hello", "fields": [], "uncertain": []}\n```'),
    )
    parsed, metadata = call_ollama(config, image, "transcribe")
    assert parsed["transcript"] == "hello"
    assert metadata["model"] == "gemma4:31b-cloud"
    assert metadata["provider"] == "ollama"


def test_ollama_call_budget_and_http_are_bounded(tmp_path):
    image = tmp_path / "page-0001.png"
    image.write_bytes(PAGE_PNG)

    oversize_config = _config(
        max_output_bytes=16,
        transport=_ollama_transport('{"transcript": "' + "x" * 64 + '"}'),
    )
    with pytest.raises(ProviderError, match="exceeds"):
        call_ollama(oversize_config, image, "p")

    reject_config = _config(
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with pytest.raises(ProviderError, match="HTTP 500"):
        call_ollama(reject_config, image, "p")

    empty_config = _config(transport=_ollama_transport(""))
    with pytest.raises(ProviderError, match="no usable content"):
        call_ollama(empty_config, image, "p")


# --- gemini -----------------------------------------------------------------

def test_gemini_call_parses_parts(tmp_path):
    image = tmp_path / "page-0001.png"
    image.write_bytes(PAGE_PNG)
    config = _config(
        provider="gemini", model="gemini-2.5-flash", base_url="https://his.example",
        api_key="k",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "k"
        body = json.loads(request.content)
        assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"transcript": "ok"}'}]}}]
        })

    config = dataclasses.replace(config, transport=httpx.MockTransport(handler))
    parsed, metadata = call_gemini(config, image, "p")
    assert parsed == {"transcript": "ok"}
    assert metadata["provider"] == "gemini"


# --- openai -----------------------------------------------------------------

class _StubMessage:
    content = '{"transcript": "from openai", "fields": [], "uncertain": []}'


class _StubCompletion:
    choices = [type("C", (), {"message": _StubMessage()})()]


class _StubChat:
    completions = type("Completions", (), {"create": staticmethod(lambda **kwargs: _StubCompletion())})()


class _StubResponse:
    chat = _StubChat()


def test_openai_call_is_wired(tmp_path, monkeypatch):
    image = tmp_path / "page-0001.png"
    image.write_bytes(PAGE_PNG)
    config = _config(
        provider="openai", model="gpt-4o-mini", base_url=None,
        api_key="sk-test",
    )
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: _StubResponse())
    parsed, metadata = call_openai(config, image, "p")
    assert parsed["transcript"] == "from openai"
    assert metadata["provider"] == "openai"


def test_get_provider_rejects_unknown_names():
    with pytest.raises(ValueError, match="Unknown VLM provider"):
        get_provider("llama.cpp")
    assert get_provider("ollama").__name__ == "call_ollama"


# --- JSON extraction --------------------------------------------------------

def test_extract_json_object_handles_fences_and_prose():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('The answer:\n{"a": {"b": "}"}}') == {"a": {"b": "}"}}
    assert extract_json_object('{"a": "keep { braces } in strings"}') == {"a": "keep { braces } in strings"}
    with pytest.raises(ProviderError, match="did not contain a JSON object"):
        extract_json_object("no json here")
    with pytest.raises(ProviderError, match="unterminated"):
        extract_json_object('{"a": ')
    with pytest.raises(ProviderError, match="did not contain a JSON object"):
        extract_json_object("[1, 2, 3]")
