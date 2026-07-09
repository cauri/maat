"""Fetch + extract a news article body — the public seam over the extraction ladder.

The single-call trafilatura fetcher this module started as (#33) is now ``acquire/content.py``:
a five-rung ladder (impersonated fetch → JSON-LD/live-blog → trafilatura+readability → Apify
re-fetch → Zyte terminal rung, inert without a key). This module keeps the import path every
caller already uses — pipeline/analyse.py, serving/analyse.py, acquire/ingest.py,
acquire/history.py — so the upgrade is invisible at the call sites.

Bodies are acquisition input (extraction reads them); the reader shows extracted claims +
attribution + links, not republished article text. The lead image (og:image) is captured for
the Apple client's thumbnail/hero (#1) — display-only, served through the reader's image
proxy, never a veracity signal. ``fetch_page`` additionally returns the page's own title/date —
the Analyse surface (P14) shows the pasted article's header.
"""

from __future__ import annotations

from maat.acquire.content import FetchedPage, fetch_article, fetch_body, fetch_page

__all__ = ["FetchedPage", "fetch_article", "fetch_body", "fetch_page"]
