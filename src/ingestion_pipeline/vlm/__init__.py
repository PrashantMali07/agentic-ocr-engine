"""VLM provider layer for the test lane.

Design boundaries (Phase 05 spec, aligned with the plans):

- Providers take a rendered page image and a prompt; they return raw model
  text plus non-document metadata. They never receive OCR output and never
  route arbitrary URLs/paths or tool requests.
- Configuration comes from environment variables only; no API keys live in
  source or contracts. The configured provider and model determine where
  inference runs.
- The default provider is Ollama with ``gemma4:31b-cloud`` for current VLM
  calls. Note that `-cloud` Ollama models route through Ollama's remote
  service, which has the same data-handling implications as other hosted
  inference.

Import: ``from ingestion_pipeline.vlm import get_provider, VlmConfig``
"""

from .base import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_BYTES,
    MAX_OUTPUT_BYTES,
    ProviderError,
    extract_json_object,
    load_bounded_image,
)
from .config import VlmConfig
from .providers import (
    PROVIDERS,
    call_gemini,
    call_ollama,
    call_openai,
    get_provider,
)

__all__ = [
    "IMAGE_EXTENSIONS",
    "MAX_OUTPUT_BYTES",
    "MAX_IMAGE_BYTES",
    "ProviderError",
    "extract_json_object",
    "load_bounded_image",
    "VlmConfig",
    "PROVIDERS",
    "call_openai",
    "call_gemini",
    "call_ollama",
    "get_provider",
]
