"""Eval-on-change: run the golden corpus through the pipeline with the CURRENT active prompts
(operator overrides from the store, else the in-code seeds) and score it.

Live LLM calls — this costs money, so it is a deliberate command. Exits non-zero if a golden
check fails, so an edited prompt that regressed the goldens is caught.

Run: make eval-prompt   (or uv run python scripts/eval_prompt.py)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.eval_prompt import eval_goldens, summary

ROOT = Path(__file__).resolve().parents[2]


async def _active_prompts() -> dict[str, str]:
    pool = await get_pool()
    try:
        return {
            k: await prompts.active_text(pool, k, prompts.seed_default(k))
            for k in ("extract", "classify", "extremity")
        }
    finally:
        await pool.close()


def main() -> int:
    load_dotenv(ROOT / ".env")
    t = asyncio.run(_active_prompts())
    report = eval_goldens(
        extract_prompt=t["extract"], classify_prompt=t["classify"], extremity_prompt=t["extremity"]
    )
    print("── eval-on-change · golden corpus with the active prompts ──")
    for s in report["stories"]:
        print(f"  {'✓' if s.ok else '✗'} {s.name}: {s.fact[:54]}")
        for c in s.checks:
            print(f"      {'✓' if c.ok else '✗'} {c.field}: {c.detail}")
    print(summary(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
