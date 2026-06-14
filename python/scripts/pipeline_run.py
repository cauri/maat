"""Run the pipeline so far — extract -> classify — on a sample article (manual; live API).

Run: uv run python scripts/pipeline_run.py
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from maat.pipeline.classify import classify_claims
from maat.pipeline.extract import extract_claims

ARTICLE = """TITLE: Iran Suspends Talks with U.S. Amid Israel's Attacks on Lebanon

Iranian diplomats have suspended talks with the United States after warning that Israel's attacks on Lebanon and the Gaza Strip could doom ongoing ceasefire negotiations with the Trump administration. Iranian Foreign Minister Abbas Araghchi said Monday that the U.S. had already violated its ceasefire with Iran when it imposed a naval siege on Iranian ports. He also said Israel's attacks on Lebanon constituted a ceasefire violation on a separate front."""


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    claims = extract_claims(ARTICLE, source_metadata="Democracy Now, 2026-06-02", language="en")
    claims = classify_claims(claims, article_text=ARTICLE)
    print(f"{len(claims)} claims (extract -> classify):\n")
    for i, c in enumerate(claims, 1):
        syn = " +synthesis" if c.is_synthesis else ""
        hor = f" horizon={c.horizon!r}" if c.horizon else ""
        print(f"{i:>2}. [{c.voice}/{c.kind}{syn}]{hor} speaker={c.speaker or '—'}\n    {c.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
