"""VLM provider configuration from environment variables.

Secrets and endpoints never appear in source, job records, or workflow IDs
and this module never logs their values. Note that `-cloud` Ollama models
route through Ollama's remote service too.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .base import MAX_IMAGE_BYTES, MAX_OUTPUT_BYTES

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"

# The active test-lane provider stays Ollama by default (currently
# gemma4:31b-cloud, which is remote-routed).
ALLOWED_PROVIDERS = ("ollama", "openai", "gemini")
DEFAULT_PROVIDER = "ollama"
DEFAULT_PROVIDER_MODEL = {
    "ollama": "gemma4:31b-cloud",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}


@dataclass(frozen=True)
class VlmConfig:
    provider: str
    model: str
    base_url: str | None
    api_key: str | None
    timeout_seconds: int = 180
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_image_bytes: int = MAX_IMAGE_BYTES
    transport: object = field(default=None, repr=False)

    @property
    def is_hosted(self) -> bool:
        return self.provider in {"openai", "gemini"} or self.model.endswith("-cloud")

    @property
    def provenance(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "provider_kind": "hosted" if self.is_hosted else "local",
        }

    @staticmethod
    def from_env(
        provider: str | None = None,
        model: str | None = None,
        transport: object = None,
    ) -> "VlmConfig":
        """Build configuration for one call.

        ``provider``/``model`` come from the CLI; when unset, environment
        variables (with code defaults) decide. ``transport`` is injectable
        for tests only.
        """
        provider = (provider or os.getenv("VLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError(f"Unknown VLM provider {provider!r}; expected one of {ALLOWED_PROVIDERS}")
        model = (model or os.getenv("VLM_MODEL") or DEFAULT_PROVIDER_MODEL[provider]).strip()
        if not model or len(model) > 128:
            raise ValueError("A VLM model name between 1 and 128 characters is required")
        timeout = max(1, min(1800, int(os.getenv("VLM_TIMEOUT_SECONDS", "180"))))
        max_output = max(1024, min(MAX_OUTPUT_BYTES, int(os.getenv("VLM_MAX_OUTPUT_BYTES", str(MAX_OUTPUT_BYTES)))))
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY is required for the openai provider")
            return VlmConfig(
                provider=provider,
                model=model,
                base_url=os.getenv("OPENAI_BASE_URL") or None,
                api_key=api_key,
                timeout_seconds=timeout,
                max_output_bytes=max_output,
                transport=transport,
            )
        if provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) is required for the gemini provider")
            gemini_host = os.getenv("GEMINI_HOST") or "https://generativelanguage.googleapis.com"
            return VlmConfig(
                provider=provider,
                model=model,
                base_url=gemini_host.rstrip("/"),
                api_key=api_key,
                timeout_seconds=timeout,
                max_output_bytes=max_output,
                transport=transport,
            )
        ollama_host = os.getenv("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST
        return VlmConfig(
            provider=provider,
            model=model,
            base_url=ollama_host.rstrip("/"),
            api_key=os.getenv("OLLAMA_API_KEY") or None,
            timeout_seconds=timeout,
            max_output_bytes=max_output,
            transport=transport,
        )
