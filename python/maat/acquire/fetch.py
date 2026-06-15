"""Fetch + extract a news article body (+ lead image), boilerplate-stripped, via trafilatura (#33).

trafilatura is built for news main-text extraction across languages, which suits the
multilingual GDELT stream. Bodies are acquisition input (extraction reads them); the reader
shows extracted claims + attribution + links, not republished article text. The lead image
(og:image) is captured for the Apple client's thumbnail/hero (#1) — display-only, served
through the reader's image proxy, never a veracity signal.
"""

from __future__ import annotations

import trafilatura
from trafilatura.metadata import extract_metadata


def fetch_article(url: str, *, min_chars: int = 200) -> tuple[str | None, str | None]:
    """Download `url`; return ``(body, image_url)``.

    body is the boilerplate-stripped main text, or None if the download fails or is too thin.
    image_url is the article's lead image (og:image / twitter:image) when present, else None.
    """
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None, None
    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=False, favor_precision=True
    )
    if not text or len(text) < min_chars:
        return None, None
    image: str | None = None
    try:
        md = extract_metadata(downloaded)
        image = (getattr(md, "image", None) or None) if md else None
    except Exception:  # noqa: BLE001 - metadata is best-effort enrichment, never fatal
        image = None
    return text, image


def fetch_body(url: str, *, min_chars: int = 200) -> str | None:
    """Back-compat: the main-text body only (callers that don't need the image)."""
    return fetch_article(url, min_chars=min_chars)[0]
