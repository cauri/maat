"""Ingestion text hygiene (#33) — strip scraped boilerplate so titles/bodies read clean.

GDELT and RSS extract bodies through trafilatura (main-text, boilerplate-stripped). The Apify
fallback (apify.py) returns the page's own ``text``/``markdown``, which carries nav, share chrome,
markdown link/image syntax, and a repeated headline — the ``[![Link…](…)](…)``, ``# …``,
``[Strange News](/newsround/…)``, ``Share / close panel / Copy link`` cruft seen on e.g.
bbc.co.uk. These helpers normalise ANY title/body to clean prose so the reader (and claim
extraction) never sees that junk.

Pure + deterministic + idempotent. Applied at ingestion (so storage + extraction are clean going
forward) AND at serving (so articles already stored render clean too). Display hygiene only —
never a veracity signal.
"""

from __future__ import annotations

import re

# --- titles ------------------------------------------------------------------------------

# Publisher delimiters, strongest first. The trailing ``<headline><sep><publisher>`` shape is the
# near-universal news-title form ("… - BBC Newsround", "… | The Guardian"). ":" is deliberately
# NOT a delimiter — it is a normal headline device ("Happy News: stories to make you smile") and
# must never be split on.
_TITLE_SEPARATORS = (" — ", " – ", " | ", " · ", " :: ", " - ")

# Words that mark a trailing segment as a publication even when it doesn't match the source domain
# (so "… - The New York Times" strips for source nytimes.com, which the domain token alone misses).
_PUBLICATION_WORDS = frozenset({
    "news", "times", "post", "herald", "tribune", "journal", "daily", "weekly", "gazette",
    "press", "wire", "report", "review", "observer", "guardian", "mail", "express", "sun",
    "mirror", "telegraph", "chronicle", "bulletin", "dispatch", "standard", "globe", "star",
    "bbc", "cnn", "npr", "reuters", "afp", "abc", "nbc", "cbs", "pbs", "fox", "msnbc", "aljazeera",
    "newsround", "online", "live", "today", "magazine", "tv", "radio", "network", "media",
})

_TLDS = frozenset({
    "com", "org", "net", "co", "uk", "fr", "de", "es", "it", "ru", "jp", "cn", "in", "br", "ar",
    "za", "kr", "qa", "il", "pk", "sg", "au", "ca", "nz", "info", "news", "press", "tv", "io",
})


def _norm(s: str) -> str:
    """Lowercase, alphanumerics only — for tolerant brand/boilerplate matching."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _brand_tokens(source: str) -> set[str]:
    """Brand tokens from a source domain: 'bbc.co.uk' -> {'bbc'}; 'theguardian.com' ->
    {'theguardian', 'guardian'}. Used to recognise a trailing publisher segment in a title."""
    host = re.sub(r"^www\.", "", (source or "").strip().lower())
    label = host.split("/")[0].split(":")[0]
    words = [p for p in label.split(".") if p and p not in _TLDS]
    brand = max(words, key=len) if words else ""
    out = {brand} if len(brand) >= 3 else set()
    if brand.startswith("the") and len(brand) > 5:
        out.add(brand[3:])
    return out


def clean_title(title: str, source: str = "") -> str:
    """Strip a redundant publisher segment from a scraped headline (display hygiene, #33).

    'Happy News: Stories to make you smile - BBC Newsround' (source bbc.co.uk) -> 'Happy News:
    Stories to make you smile'. Conservative: only a SHORT trailing segment after a publisher
    delimiter, and only when it matches the source brand OR clearly names a publication — a real
    'A - B' headline is left alone, and ':' inside the headline is never touched.
    """
    t = (title or "").strip()
    if not t:
        return t
    brands = _brand_tokens(source)

    def _is_publisher(seg: str) -> bool:
        seg = seg.strip()
        if not seg or len(seg.split()) > 6:
            return False
        nseg = _norm(seg)
        if nseg and any(b in nseg or nseg in b for b in brands):
            return True
        return any(_norm(w) in _PUBLICATION_WORDS for w in seg.split())

    for sep in _TITLE_SEPARATORS:
        idx = t.rfind(sep)
        if idx <= 0:
            continue
        left, right = t[:idx].strip(), t[idx + len(sep):].strip()
        if len(left.split()) >= 2 and len(left) >= 8 and _is_publisher(right):
            return left
    # leading 'Publisher: '/'Publisher - <headline>' — only on a strong source-brand match
    for sep in (": ", *(_TITLE_SEPARATORS)):
        idx = t.find(sep)
        if idx <= 0:
            continue
        left, right = t[:idx].strip(), t[idx + len(sep):].strip()
        nleft = _norm(left)
        if (len(right.split()) >= 3 and len(left.split()) <= 4
                and nleft and any(b in nleft or nleft in b for b in brands)):
            return right
    return t


# --- bodies ------------------------------------------------------------------------------

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![alt](src)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")           # [text](href) -> text
_NAV_LINK_LINE = re.compile(r"\[[^\]]*\]\((?:/[^)]*|#[^)]*)\)")  # a lone link to a relative/anchor target
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_MARK = re.compile(r"^\s{0,3}(?:[*\-+]|\d+\.)\s+")
_AGO = re.compile(r"^\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago$", re.I)

# Standalone nav / share / chrome lines (matched on _norm) dropped wholesale.
_BOILERPLATE_NORM = frozenset(_norm(s) for s in {
    "share", "share page", "share this", "close", "close panel", "copy link", "copied",
    "read description", "read more", "about sharing", "published", "watch our other weekly catch-ups",
    "advertisement", "advertise with us", "sign up", "subscribe", "subscribe now", "newsletter",
    "skip to content", "menu", "search", "follow us", "related", "related stories", "related topics",
    "more on this story", "comments", "image source", "image caption", "media caption",
    "getty images", "view comments", "most read", "recommended", "sponsored", "share page",
})


def clean_body(text: str, *, title: str = "") -> str:
    """Normalise a scraped article body to clean prose (#33).

    Drops markdown image/link syntax, headings, list bullets, bare URLs, the publisher's nav/share
    chrome, timestamp lines ("1 day ago"), and a duplicated leading headline — the cruft the Apify
    fallback leaks. trafilatura output passes through essentially untouched. Idempotent.
    """
    if not text:
        return text or ""
    text = _MD_IMAGE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    ntitle = _norm(title)

    kept: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            kept.append("")
            continue
        if _NAV_LINK_LINE.fullmatch(line):            # a lone relative/anchor link == navigation
            continue
        line = _LIST_MARK.sub("", _HEADING.sub("", line))   # '# …' / '* …' markers
        line = _BARE_URL.sub("", _MD_LINK.sub(r"\1", line)).strip()  # unwrap links, drop URLs
        if not line or _AGO.match(line):
            continue
        key = _norm(line)
        if not key or key in _BOILERPLATE_NORM:
            continue
        if ntitle and (key == ntitle or key.startswith(ntitle)):   # duplicated headline (+ 'smileClose')
            continue
        kept.append(line)

    out: list[str] = []
    for line in kept:                                  # collapse dup lines + runs of blanks
        if line and out[-1:] == [line]:
            continue
        if line == "" and out[-1:] == [""]:
            continue
        out.append(line)
    return "\n".join(out).strip()


def clean_article(title: str, body: str, source: str = "") -> tuple[str, str]:
    """Clean a (title, body) pair for one source — the single call each acquire driver makes
    before publishing ``article.ingested``, and that the serving layer reuses on read so
    already-stored articles render clean too. Returns ``(clean_title, clean_body)``."""
    t = clean_title(title, source)
    return t, clean_body(body, title=t)
