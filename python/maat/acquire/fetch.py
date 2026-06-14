"""Fetch + extract a news article body, boilerplate-stripped, via trafilatura (#33).

trafilatura is built for news main-text extraction across languages, which suits the
multilingual GDELT stream. Bodies are acquisition input (extraction reads them); the reader
shows extracted claims + attribution + links, not republished article text.
"""

from __future__ import annotations

import trafilatura


def fetch_body(url: str, *, min_chars: int = 200) -> str | None:
    """Download `url` and extract the main article text; None if it fails or is too thin."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=False, favor_precision=True
    )
    if not text or len(text) < min_chars:
        return None
    return text
