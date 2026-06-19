"""Thin alias for ``acquire.py --source rss`` (#290).

Kept so the prod clock (deploy/docker-compose.prod.yml) and ``make acquire-rss`` keep working
unchanged; the real RSS orchestration lives in ``maat/acquire/drivers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from maat.acquire.drivers import acquire

ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    asyncio.run(acquire("rss", root=ROOT))
