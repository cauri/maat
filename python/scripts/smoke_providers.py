"""Smoke-test the provider seam against live APIs: Claude + Mistral chat + embeddings.

Run: `make py-smoke` (or `uv run python scripts/smoke_providers.py`). Not in CI — it
needs live keys and costs money; CI stays deterministic (DECISIONS D16).
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from maat.providers.seam import claude_complete, mistral_complete, mistral_embed

# Load repo-root .env regardless of CWD (python/scripts/ -> repo root is parents[2]).
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def main() -> int:
    ok = True

    try:
        r = claude_complete("Reply with the single word: OK", max_tokens=16)
        print(f"Claude   [{r.model}] -> {r.text.strip()!r}")
    except Exception as exc:  # noqa: BLE001 - smoke test surfaces any failure
        print(f"Claude   FAILED: {exc}")
        ok = False

    try:
        r = mistral_complete("Reply with the single word: OK", max_tokens=16)
        print(f"Mistral  [{r.model}] -> {r.text.strip()!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"Mistral  FAILED: {exc}")
        ok = False

    try:
        vecs = mistral_embed(["hello world", "bonjour le monde"])
        print(f"Embed    [mistral-embed] -> {len(vecs)} vectors, dim={len(vecs[0])}")
    except Exception as exc:  # noqa: BLE001
        print(f"Embed    FAILED: {exc}")
        ok = False

    print("OK" if ok else "FAILURES — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
