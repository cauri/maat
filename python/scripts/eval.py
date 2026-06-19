"""Run the eval harness over the current projections (after a pipeline pass).

Reports per-stage metrics + a golden-regression check on the fixtures; exits non-zero if a
golden check fails. Run: `make eval` (needs a populated store — ingest + agents + corroborate).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat.evals import evaluate, load_expectations

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pool = await get_pool()
    clusters = [
        dict(r)
        for r in await pool.fetch(
            "select fact, sources, originators, independent_originators, has_primary, "
            "confidence, extremity from clusters"
        )
    ]
    claims = [dict(r) for r in await pool.fetch("select kind from claims")]
    await pool.close()

    report = evaluate(clusters, claims, load_expectations())
    m = report["metrics"]
    print("── metrics ──")
    print(f"  claims {m['claims']}  {m['claim_kinds']}")
    print(f"  clusters {m['clusters']}  · primary {m['with_primary']}  · extraordinary {m['extraordinary']}")
    print(f"  confidence mean {m['confidence_mean']}  · labels {m['labels']}")
    print("── golden checks ──")
    for s in report["stories"]:
        print(f"  {'✓' if s.ok else '✗'} {s.name}: {s.fact[:54]}")
        for c in s.checks:
            print(f"      {'✓' if c.ok else '✗'} {c.field}: {c.detail}")
    print(f"\n{'PASS' if report['passed'] else 'FAIL'}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
