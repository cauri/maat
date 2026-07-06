"""Fetch + extract a news article body (+ lead image), boilerplate-stripped, via trafilatura (#33).

trafilatura is built for news main-text extraction across languages, which suits the
multilingual GDELT stream. Bodies are acquisition input (extraction reads them); the reader
shows extracted claims + attribution + links, not republished article text. The lead image
(og:image) is captured for the Apple client's thumbnail/hero (#1) — display-only, served
through the reader's image proxy, never a veracity signal.

``fetch_page`` additionally returns the page's own title/date — the Analyse surface (P14)
shows the pasted article's header, which the acquisition path never needed.
"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura
from trafilatura.metadata import extract_metadata


@dataclass(frozen=True)
class FetchedPage:
    """One fetched article page — main text plus best-effort display metadata."""

    body: str
    title: str | None = None
    image: str | None = None  # lead image (og:image) — display-only, never a veracity signal
    date: str | None = None   # publication date (ISO) when the page states one


def fetch_page(url: str, *, min_chars: int = 200) -> FetchedPage | None:
    """Download `url` → FetchedPage, or None if the download fails or the text is too thin."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=False, favor_precision=True
    )
    if not text or len(text) < min_chars:
        return None
    title = image = date = None
    try:
        md = extract_metadata(downloaded)
        if md:
            title = getattr(md, "title", None) or None
            image = getattr(md, "image", None) or None
            date = getattr(md, "date", None) or None
    except Exception:  # noqa: BLE001 - metadata is best-effort enrichment, never fatal
        pass
    return FetchedPage(body=text, title=title, image=image, date=date)


def fetch_article(url: str, *, min_chars: int = 200) -> tuple[str | None, str | None]:
    """Download `url`; return ``(body, image_url)``.

    body is the boilerplate-stripped main text, or None if the download fails or is too thin.
    image_url is the article's lead image (og:image / twitter:image) when present, else None.
    """
    page = fetch_page(url, min_chars=min_chars)
    return (page.body, page.image) if page else (None, None)


def fetch_body(url: str, *, min_chars: int = 200) -> str | None:
    """Back-compat: the main-text body only (callers that don't need the image)."""
    return fetch_article(url, min_chars=min_chars)[0]
