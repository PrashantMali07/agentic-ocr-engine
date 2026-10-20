"""Shared guards and helpers for VLM provider calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Bounded media and response budgets. The response cap mirrors
# scripts/prepare_vlm_review.py so a runaway model cannot blow up the
# review pipeline; the image cap matches its rendered-page budget.
MAX_IMAGE_BYTES = 60 * 1024 * 1024
MAX_OUTPUT_BYTES = 2_000_000

IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}

class ProviderError(RuntimeError):
    """A bounded, operator-readable failure from a VLM provider."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def load_bounded_image(image_path: Path) -> tuple[bytes, str]:
    """Read a page image and its media type under the size budget.

    Returns raw bytes plus a normalised media (MIME) type; callers base64
    encode as required by their provider. Symlinks are refused so evidence
    files cannot be redirected through the filesystem."""
    if not image_path.is_file() or image_path.is_symlink():
        raise ProviderError("image_missing", f"Page image not found or unsafe: {image_path.name}")
    media_type = IMAGE_EXTENSIONS.get(image_path.suffix.lower())
    if media_type is None:
        raise ProviderError("image_unsupported", f"Unsupported page image type: {image_path.suffix}")
    size = image_path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ProviderError("image_too_large", f"Page image exceeds {MAX_IMAGE_BYTES} bytes")
    data = image_path.read_bytes()
    if len(data) != size:
        raise ProviderError("image_changed", "Page image changed while being read")
    return data, media_type


def extract_json_object(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> dict:
    """Extract the first JSON object from model text, ignoring code fences.

    Vision models frequently wrap their answer in ```json fences or add
    prose; the review tooling (scripts/prepare_vlm_review.py) requires one
    clean JSON object on stdout.
    """
    if len(text.encode("utf-8")) > max_bytes:
        raise ProviderError("output_limit_exceeded", f"VLM response exceeds {max_bytes} bytes")
    candidate = text.strip()
    # Reading-order transcript payloads are what the model should have
    # produced; find the first balanced outermost object as a fallback.
    start = candidate.find("{")
    if start == -1:
        raise ProviderError("not_json", "VLM response did not contain a JSON object")
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end == -1:
        raise ProviderError("not_json", "VLM response JSON object is unterminated")
    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError("not_json", f"VLM response is not valid JSON ({exc.msg} at char {exc.pos})") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("not_json", "VLM response must be a JSON object")
    return parsed


def response_metadata(provider: str, model: str, raw_text: str, elapsed_ms: int) -> dict:
    """Provider provenance metadata. Contains no document content; the SHA
    binds the exact raw response for the review manifest without retaining
    the text itself beyond the caller's own lifecycle."""
    return {
        "provider": provider,
        "model": model,
        "response_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "response_bytes": len(raw_text.encode("utf-8")),
        "elapsed_ms": elapsed_ms,
    }

