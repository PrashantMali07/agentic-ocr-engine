"""The three VLM provider call functions: Ollama, OpenAI, Gemini.

Each provider: loads the page image under budget, base64 encodes it for one
bounded request, enforces the timeout/response budget, extracts one JSON
object from the model text, and returns it with provenance metadata. They
receive only the prompt; OCR output is never attached.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .base import (
    ProviderError,
    extract_json_object,
    load_bounded_image,
    response_metadata,
)
from .config import VlmConfig

TranscribeResult = tuple[dict, dict]  # (parsed JSON object, provider metadata)


def _client(config: VlmConfig, provider: str) -> httpx.Client:
    if config.transport is not None:  # test injection only
        return httpx.Client(transport=config.transport, timeout=config.timeout_seconds)  # type: ignore[arg-type]
    return httpx.Client(timeout=config.timeout_seconds, trust_env=False)


def call_ollama(config: VlmConfig, image_path: Path, prompt: str) -> TranscribeResult:
    """Call an Ollama chat endpoint with a vision model.

    The current test-lane model is ``gemma4:31b-cloud`` — a `-cloud` tag
    routes through Ollama's remote servers, so treat it as hosted data
    handling even though it speaks the local Ollama API."""
    image_bytes, media_type = load_bounded_image(image_path)
    body = {
        "model": config.model,
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image_bytes).decode("ascii")],
            }
        ],
    }
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    started = time.monotonic()
    try:
        with _client(config, "ollama") as client:
            response = client.post(
                f"{config.base_url}/api/chat",
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"Ollama call exceeded {config.timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("transport", f"Ollama call failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise ProviderError("provider_rejected", f"Ollama returned HTTP {response.status_code}")
    payload = response.json()
    content = (payload.get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("empty_response", "Ollama returned no usable content")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    parsed = extract_json_object(content, config.max_output_bytes)
    return parsed, response_metadata("ollama", config.model, content, elapsed_ms)


def call_openai(config: VlmConfig, image_path: Path, prompt: str) -> TranscribeResult:
    """Call OpenAI chat completions with image input via the installed SDK."""
    image_bytes, media_type = load_bounded_image(image_path)
    data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    started = time.monotonic()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=config.timeout_seconds)
        completion = client.chat.completions.create(
            model=config.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0,
        )
    except ProviderError:
        raise
    except Exception as exc:  # SDK/network/HTTP surface varies by version
        raise ProviderError("provider_rejected", f"OpenAI call failed: {type(exc).__name__}") from exc
    content = (completion.choices[0].message.content or "").strip() if completion.choices else ""
    if not content:
        raise ProviderError("empty_response", "OpenAI returned no usable content")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    parsed = extract_json_object(content, config.max_output_bytes)
    return parsed, response_metadata("openai", config.model, content, elapsed_ms)


def call_gemini(config: VlmConfig, image_path: Path, prompt: str) -> TranscribeResult:
    """Call a Gemini generateContent endpoint with an inline page image."""
    image_bytes, media_type = load_bounded_image(image_path)
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {
                        "mime_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }},
                ]
            }
        ],
        "generationConfig": {"temperature": 0},
    }
    started = time.monotonic()
    try:
        with _client(config, "gemini") as client:
            response = client.post(
                f"{config.base_url}/v1beta/models/{config.model}:generateContent",
                json=body,
                headers={"x-goog-api-key": config.api_key or ""},
            )
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"Gemini call exceeded {config.timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("transport", f"Gemini call failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise ProviderError("provider_rejected", f"Gemini returned HTTP {response.status_code}")
    payload = response.json()
    candidate = (payload.get("candidates") or [])[0].get("content", {}) if payload.get("candidates") else {}
    parts = candidate.get("parts") or []
    content = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    if not content:
        raise ProviderError("empty_response", "Gemini returned no usable content")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    parsed = extract_json_object(content, config.max_output_bytes)
    return parsed, response_metadata("gemini", config.model, content, elapsed_ms)


# Registry: name -> single bounded call signature.
PROVIDERS: dict[str, Callable[..., TranscribeResult]] = {
    "ollama": call_ollama,
    "openai": call_openai,
    "gemini": call_gemini,
}


def get_provider(provider: str) -> Callable[..., TranscribeResult]:
    try:
        return PROVIDERS[provider]
    except KeyError as exc:
        raise ValueError(f"Unknown VLM provider {provider!r}; expected one of {sorted(PROVIDERS)}") from exc
