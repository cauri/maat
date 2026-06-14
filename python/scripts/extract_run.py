"""Run the Assessor extraction stage on a sample article (manual; hits the live API).

Uses a different article than the in-prompt example, so it's a real test of the prompt.
Run: uv run python scripts/extract_run.py
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from maat.pipeline.extract import extract_claims

ARTICLE = """TITLE: Iran Suspends Talks with U.S. Amid Israel's Attacks on Lebanon

Iranian diplomats have suspended talks with the United States after warning that Israel's attacks on Lebanon and the Gaza Strip could doom ongoing ceasefire negotiations with the Trump administration. Iranian Foreign Minister Abbas Araghchi said Monday that the U.S. had already violated its ceasefire with Iran when it imposed a naval siege on Iranian ports. He also said Israel's attacks on Lebanon constituted a ceasefire violation on a separate front."""


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    claims = extract_claims(ARTICLE, source_metadata="Democracy Now, 2026-06-02", language="en")
    print(f"{len(claims)} claims:\n")
    for i, c in enumerate(claims, 1):
        chain = " → ".join(c.relay_chain) if c.relay_chain else "—"
        head = "  [HEADLINE]" if c.in_headline else ""
        print(f"{i:>2}. [{c.voice}] speaker={c.speaker or '—'} | chain={chain}{head}\n    {c.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
