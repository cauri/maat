"""Thin alias for ``acquire.py --source newsdata`` (#290).

Kept so the prod clock (deploy/docker-compose.prod.yml) and ``make acquire-newsdata`` keep working
unchanged; the real NewsData orchestration lives in ``maat/acquire/drivers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from maat.acquire.drivers import acquire

ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    asyncio.run(acquire("newsdata", root=ROOT))
