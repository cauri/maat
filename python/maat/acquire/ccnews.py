"""CC-News (Common Crawl) — historical news archive as a backfill source (#237, P3).

Common Crawl's CC-NEWS dataset is a large MULTILINGUAL archive of news articles (monthly WARC
files since 2016) — far broader and more globally diverse than a GDELT replay, which makes it
ideal for de-slanted reputation TRAINING (extends #40/#37): pull history, cap each
(language, country) stratum so the Anglophone majors can't dominate, and feed the normal
extract -> classify -> corroborate -> reputation path tagged ``backfill``.

Each CC-NEWS WARC ``response`` record carries the article URL, the raw HTML, and a CLD2 language
label (``WARC-Identified-Content-Language``) — so unlike GDELT we get **body + language in one
pass, no separate fetch**. Body/title/image come from trafilatura (the same extractor as
``fetch.py``); country is guessed from the TLD.

Public, no key. The path index for a month is
``https://data.commoncrawl.org/crawl-data/CC-NEWS/{YYYY}/{MM}/warc.paths.gz`` and each listed
path is a WARC.gz at ``https://data.commoncrawl.org/{path}``.
"""

from __future__ import annotations

import gzip
import io
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import py3langid
import trafilatura
from trafilatura.metadata import extract_metadata
from warcio.archiveiterator import ArchiveIterator

CC_BASE = "https://data.commoncrawl.org"
_PATHS = CC_BASE + "/crawl-data/CC-NEWS/{year:04d}/{month:02d}/warc.paths.gz"

# CLD2 ISO-639-3 -> 2-letter, so CC-News strata line up with GDELT's 2-letter language codes.
_LANG3TO2 = {
    "eng": "en", "fra": "fr", "spa": "es", "deu": "de", "ger": "de", "rus": "ru", "zho": "zh",
    "cmn": "zh", "ara": "ar", "por": "pt", "ita": "it", "jpn": "ja", "kor": "ko", "hin": "hi",
    "tur": "tr", "ell": "el", "ind": "id", "ukr": "uk", "nld": "nl", "pol": "pl", "vie": "vi",
    "tha": "th", "fas": "fa", "heb": "he", "swe": "sv", "nor": "no", "fin": "fi", "ces": "cs",
}

# TLD -> ISO-3166-1 alpha-2 (a pragmatic subset; unknown -> "" = its own 'unknown' stratum).
_TLD_COUNTRY = {
    "co.uk": "GB", "uk": "GB", "de": "DE", "fr": "FR", "es": "ES", "it": "IT", "nl": "NL",
    "ru": "RU", "com.cn": "CN", "cn": "CN", "co.jp": "JP", "jp": "JP", "kr": "KR", "co.in": "IN",
    "in": "IN", "com.br": "BR", "br": "BR", "tr": "TR", "gr": "GR", "co.id": "ID", "id": "ID",
    "ua": "UA", "pl": "PL", "se": "SE", "no": "NO", "com.au": "AU", "au": "AU", "ca": "CA",
    "co.za": "ZA", "za": "ZA", "ng": "NG", "ke": "KE", "eg": "EG", "com.ar": "AR", "ar": "AR",
    "com.mx": "MX", "mx": "MX", "sa": "SA", "ae": "AE", "qa": "QA", "pk": "PK", "ph": "PH",
    "vn": "VN", "th": "TH", "my": "MY", "sg": "SG", "pt": "PT", "ir": "IR", "il": "IL",
}


@dataclass(frozen=True)
class CCNewsArticle:
    url: str
    title: str
    source: str       # registrable domain (e.g. "lemonde.fr")
    language: str     # 2-letter code from the WARC CLD2 header; "" if absent
    country: str      # ISO-2 guessed from the TLD; "" if unknown
    body: str
    image: str | None
    seendate: str     # WARC-Date (ISO-8601) — provenance for the backfill prior


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def country_of(domain: str) -> str:
    """Best-effort ISO-2 country from a domain's TLD ("" if unknown). Pure."""
    labels = (domain or "").split(".")
    for n in (2, 1):  # prefer a two-label TLD (co.uk) over a one-label one (uk)
        if len(labels) > n:
            suffix = ".".join(labels[-n:])
            if suffix in _TLD_COUNTRY:
                return _TLD_COUNTRY[suffix]
    return ""


def normalise_lang(raw: str) -> str:
    """CC-NEWS ``WARC-Identified-Content-Language`` (CLD2, comma-sep by confidence) -> 2-letter."""
    first = (raw or "").split(",")[0].strip().lower()
    if not first:
        return ""
    return _LANG3TO2.get(first, first[:2])


def detect_lang(text: str) -> str:
    """Detect a 2-letter language from the article text. CC-NEWS records frequently OMIT the
    CLD2 ``WARC-Identified-Content-Language`` header (verified on 2026-05 segments — every record
    lacked it), and an unknown language collapses everything into one stratum, defeating the
    de-slant. Offline + deterministic via py3langid; "" when there's too little text to judge."""
    t = (text or "").strip()
    if len(t) < 20:
        return ""
    try:
        code, _ = py3langid.classify(t[:1000])
        return (code or "").strip().lower()
    except Exception:  # noqa: BLE001 - detection is best-effort enrichment, never fatal
        return ""


def warc_paths(year: int, month: int, *, client: httpx.Client | None = None, limit: int = 0) -> list[str]:
    """Fetch the CC-NEWS WARC path index for a month (``limit`` > 0 truncates)."""
    own = client is None
    c = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        r = c.get(_PATHS.format(year=year, month=month))
        r.raise_for_status()
        paths = gzip.decompress(r.content).decode().split()
    finally:
        if own:
            c.close()
    return paths[:limit] if limit > 0 else paths


def iter_warc(stream: io.IOBase, *, limit: int = 0, min_chars: int = 400) -> Iterator[CCNewsArticle]:
    """Yield ``CCNewsArticle`` from an open WARC(.gz) byte stream (warcio handles the gzip).

    Pure over the stream — the caller supplies the bytes (a live download or a test fixture), so
    this is unit-testable with no network. Skips non-response / non-HTML records and thin bodies.
    """
    yielded = 0
    for rec in ArchiveIterator(stream):
        if rec.rec_type != "response":
            continue
        url = rec.rec_headers.get_header("WARC-Target-URI") or ""
        if not url:
            continue
        ctype = (rec.http_headers and rec.http_headers.get_header("Content-Type")) or ""
        if "html" not in ctype.lower():
            continue
        raw = rec.content_stream().read()
        if not raw:
            continue
        html = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        body = trafilatura.extract(
            html, include_comments=False, include_tables=False, favor_precision=True
        )
        if not body or len(body) < min_chars:
            continue
        title, image = "", None
        try:
            md = extract_metadata(html)
            if md:
                title = (md.title or "").strip()
                image = getattr(md, "image", None) or None
        except Exception:  # noqa: BLE001 - metadata is best-effort enrichment, never fatal
            pass
        domain = _domain(url)
        language = normalise_lang(rec.rec_headers.get_header("WARC-Identified-Content-Language") or "")
        if not language:  # CC-NEWS often omits the CLD2 header — detect from the body instead
            language = detect_lang(body)
        yield CCNewsArticle(
            url=url,
            title=title,
            source=domain,
            language=language,
            country=country_of(domain),
            body=body,
            image=image,
            seendate=rec.rec_headers.get_header("WARC-Date") or "",
        )
        yielded += 1
        if limit and yielded >= limit:
            return


class _IterReader(io.RawIOBase):
    """Adapt a byte-chunk iterator (httpx ``iter_raw``) into a ``.read(n)`` stream for warcio, so
    we decompress + parse incrementally and can STOP after ``limit`` records — CC-NEWS WARCs are
    ~1 GB each, so downloading the whole file just to take a few hundred articles is wasteful."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._it = chunks
        self._buf = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            rest = b"".join(self._it)
            out, self._buf = self._buf + rest, b""
            return out
        while len(self._buf) < size:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = self._buf[:size], self._buf[size:]
        return out


def fetch_warc(warc_path: str, *, client: httpx.Client | None = None, limit: int = 0) -> list[CCNewsArticle]:
    """Stream one CC-NEWS WARC and parse it into articles, stopping after ``limit`` (0 = all).

    Streams rather than buffering the full ~1 GB file: warcio reads through ``_IterReader`` and we
    break early once ``limit`` articles are yielded, so only a fraction of the WARC is downloaded.
    """
    url = warc_path if warc_path.startswith("http") else f"{CC_BASE}/{warc_path.lstrip('/')}"
    own = client is None
    c = client or httpx.Client(timeout=180, follow_redirects=True)
    try:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            return list(iter_warc(_IterReader(r.iter_raw()), limit=limit))
    finally:
        if own:
            c.close()
