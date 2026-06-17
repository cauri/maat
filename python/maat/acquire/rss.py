"""RSS/Atom acquisition (#238) — a balanced, multipolar set of outlet feeds.

Publisher-direct and reliable: no GDELT 429s, no Google-redirect games, clean provenance. The
feed LIST is the diversity lever — deliberately spread across regions, languages, and blocs so
it *strengthens* the de-US balance instead of re-centering on the Anglophone majors. Each feed
is tagged with country / language and an ALIGNMENT (``independent`` | ``public`` | ``state``);
state-aligned outlets (RT, CGTN, …) are ingested but flagged, so the independence / reputation
layer can weight them and never count them as independent corroboration (#41/#187).

The default set below is operator-editable via ``config/feeds.txt``
(``url | country | language | alignment | label``, ``#`` comments).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import httpx


@dataclass(frozen=True)
class Feed:
    url: str
    country: str       # ISO-3166-1 alpha-2
    language: str      # 2-letter hint (the feed/item language wins when present)
    alignment: str     # independent | public | state
    label: str


@dataclass(frozen=True)
class FeedItem:
    url: str
    title: str
    source: str        # registrable domain
    language: str
    country: str
    alignment: str


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


# A deliberately multipolar starter set — spread across blocs/languages, NOT Anglophone-weighted.
# alignment: independent (commercial/independent), public (public-service broadcaster), state
# (state-controlled — ingested but down-weighted by the independence layer, never independent
# corroboration). Operator-editable via config/feeds.txt.
DEFAULT_FEEDS: tuple[Feed, ...] = (
    # — Anglophone (kept proportionate, not dominant) —
    Feed("https://feeds.bbci.co.uk/news/world/rss.xml", "GB", "en", "public", "BBC World"),
    Feed("https://www.theguardian.com/world/rss", "GB", "en", "independent", "The Guardian"),
    Feed("https://feeds.npr.org/1001/rss.xml", "US", "en", "public", "NPR"),
    # — Europe (multiple languages: en/fr/es/de) —
    Feed("https://rss.dw.com/rdf/rss-en-all", "DE", "en", "public", "Deutsche Welle"),
    Feed("https://www.tagesschau.de/index~rss2.xml", "DE", "de", "public", "Tagesschau"),
    Feed("https://www.lemonde.fr/rss/une.xml", "FR", "fr", "independent", "Le Monde"),
    Feed("https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", "ES", "es", "independent", "El País"),
    Feed("https://www.spiegel.de/international/index.rss", "DE", "en", "independent", "Der Spiegel Intl"),
    # — Middle East —
    Feed("https://www.aljazeera.com/xml/rss/all.xml", "QA", "en", "public", "Al Jazeera"),
    Feed("https://www.timesofisrael.com/feed/", "IL", "en", "independent", "Times of Israel"),
    # — South / East / SE Asia (incl. native ja) —
    Feed("https://www.thehindu.com/news/international/feeder/default.rss", "IN", "en", "independent", "The Hindu"),
    Feed("https://en.yna.co.kr/RSS/news.xml", "KR", "en", "public", "Yonhap"),
    Feed("https://www3.nhk.or.jp/rss/news/cat0.xml", "JP", "ja", "public", "NHK"),
    Feed("https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", "SG", "en", "public", "CNA"),
    Feed("https://www.dawn.com/feeds/home", "PK", "en", "independent", "Dawn"),
    # — Africa / Latin America (native es/pt) —
    Feed("https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf", "ZA", "en", "independent", "allAfrica"),
    Feed("https://feeds.folha.uol.com.br/emcimadahora/rss091.xml", "BR", "pt", "independent", "Folha de S.Paulo"),
    Feed("https://www.infobae.com/arc/outboundfeeds/rss/", "AR", "es", "independent", "Infobae"),
    # — Russia: independent (native ru) AND state, so the bloc isn't only Kremlin framing —
    Feed("https://meduza.io/rss/all", "RU", "ru", "independent", "Meduza"),
    Feed("https://tass.com/rss/v2.xml", "RU", "en", "state", "TASS"),
    Feed("https://www.rt.com/rss/news/", "RU", "en", "state", "RT"),
    # — Other state-aligned (FLAGGED — broaden worldview, never independent corroboration) —
    Feed("https://www.cgtn.com/subscribe/rss/section/world.xml", "CN", "en", "state", "CGTN"),
    Feed("http://www.chinadaily.com.cn/rss/world_rss.xml", "CN", "en", "state", "China Daily"),
)


def load_feeds(path: Path | None = None) -> list[Feed]:
    """Operator feed list from ``config/feeds.txt`` if present, else ``DEFAULT_FEEDS``.

    Line format: ``url | country | language | alignment | label`` (``#`` comments, blanks ignored).
    """
    if path and path.exists():
        feeds: list[Feed] = []
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) >= 4 and parts[0]:
                feeds.append(Feed(
                    url=parts[0], country=parts[1], language=parts[2],
                    alignment=parts[3], label=parts[4] if len(parts) > 4 else _domain(parts[0]),
                ))
        if feeds:
            return feeds
    return list(DEFAULT_FEEDS)


def parse_feed(content: str | bytes, feed: Feed, *, limit: int = 0) -> list[FeedItem]:
    """Parse feed bytes (feedparser handles RSS 0.9–2.0, Atom, and malformed feeds). Pure."""
    parsed = feedparser.parse(content)
    feed_lang = (parsed.feed.get("language") or feed.language or "")[:2].lower()
    out: list[FeedItem] = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        out.append(FeedItem(
            url=link,
            title=(e.get("title") or "").strip(),
            source=_domain(link) or _domain(feed.url),
            language=feed_lang or feed.language,
            country=feed.country,
            alignment=feed.alignment,
        ))
        if limit and len(out) >= limit:
            break
    return out


def fetch_feed(feed: Feed, *, client: httpx.Client | None = None, limit: int = 0) -> list[FeedItem]:
    """Download + parse one feed. Raises on HTTP error (the driver skips a dead feed, logs it)."""
    own = client is None
    c = client or httpx.Client(timeout=30, follow_redirects=True,
                               headers={"User-Agent": "maat-rss/1 (+https://maat.press)"})
    try:
        r = c.get(feed.url)
        r.raise_for_status()
        return parse_feed(r.content, feed, limit=limit)
    finally:
        if own:
            c.close()
