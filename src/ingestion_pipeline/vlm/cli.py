"""CLI runner honouring the scripts/prepare_vlm_review.py runner contract.

Contract: receives ``--image PATH`` plus optional provider args, reads one
prompt from stdin, prints exactly one JSON object to stdout. Exit code is
non-zero on failure. It never receives or emits OCR output and never logs
document content.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .base import ProviderError
from .config import VlmConfig
from .providers import get_provider


def main() -> int:
    parser = argparse.ArgumentParser(description="One page -> one VLM draft response")
    parser.add_argument("--image", required=True, type=Path, help="Path to the rendered page image")
    parser.add_argument("--provider", choices=("ollama", "openai", "gemini"), default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("VLM runner failed: empty prompt", file=sys.stderr)
        return 2
    try:
        config = VlmConfig.from_env(provider=args.provider, model=args.model)
        call = get_provider(config.provider)
        parsed, metadata = call(config, args.image, prompt)
        # One JSON object on stdout; provenance goes to stderr.
        print(json.dumps(parsed, ensure_ascii=False))
        print(json.dumps(metadata), file=sys.stderr)
        return 0
    except ProviderError as exc:
        print(f"VLM runner failed: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"VLM runner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
