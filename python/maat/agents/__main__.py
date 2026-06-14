"""Run all Maat agents concurrently: `uv run python -m maat.agents`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from maat.agents.classify_agent import handle as classify_handle
from maat.agents.extract_agent import handle as extract_handle
from maat.bus import run_agent


async def _run() -> None:
    await asyncio.gather(
        run_agent("extract", "maat.events.article.ingested", extract_handle),
        run_agent("classify", "maat.events.claims.extracted", classify_handle),
    )


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
