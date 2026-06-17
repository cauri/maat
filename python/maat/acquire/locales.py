"""Per-locale acquisition config (#239) — actively pull under-represented languages/regions.

The default GDELT/RSS passes surface whatever ranks; this adds a deliberate FLOOR: for each
configured locale we pull news in that language. Two engines, in order: GDELT filtered to the
language (``sourcelang`` / ``sourcecountry``, lowercase NAMES — ``spanish`` / ``spain``), and —
when GDELT is rate-limited or empty — a paid Apify pass that Googles the locale's own-language
``terms`` and reliably returns native-language results (verified: 中文→voachinese/bbc中文,
العربية→aljazeera). Apify is what keeps the floor real, since GDELT's free API 429s under load.

Operator-editable via ``config/locales.txt`` (``language | country | label | terms``, ``#``
comments; blank country = that language globally; terms optional — falls back to the built-in).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Locale:
    language: str   # GDELT sourcelang NAME, e.g. "spanish"
    country: str    # GDELT sourcecountry NAME, e.g. "spain" ("" = that language globally)
    label: str
    terms: str = ""  # native-language search query for the Apify floor (#239); "" = skip Apify


# Deliberately weighted toward languages the Anglophone-default stream under-covers. Each carries
# a native-language query (politics / AI / technology / science / news) so the Apify floor pulls
# in-language coverage even while GDELT is throttled.
DEFAULT_LOCALES: tuple[Locale, ...] = (
    Locale("arabic", "", "Arabic", "سياسة عالمية OR ذكاء اصطناعي OR تكنولوجيا OR علوم أخبار"),
    Locale("chinese", "", "Chinese", "全球政治 OR 人工智能 OR 科技 OR 科学 新闻"),
    Locale("russian", "russia", "Russian / RU", "мировая политика OR искусственный интеллект OR технологии OR наука новости"),
    Locale("spanish", "", "Spanish", "política mundial OR inteligencia artificial OR tecnología OR ciencia noticias"),
    Locale("hindi", "india", "Hindi / IN", "वैश्विक राजनीति OR कृत्रिम बुद्धिमत्ता OR प्रौद्योगिकी OR विज्ञान समाचार"),
    Locale("portuguese", "brazil", "Portuguese / BR", "política mundial OR inteligência artificial OR tecnologia OR ciência notícias"),
    Locale("french", "", "French", "politique mondiale OR intelligence artificielle OR technologie OR science actualités"),
    Locale("german", "germany", "German / DE", "Weltpolitik OR künstliche Intelligenz OR Technologie OR Wissenschaft Nachrichten"),
    Locale("japanese", "japan", "Japanese / JP", "世界政治 OR 人工知能 OR テクノロジー OR 科学 ニュース"),
    Locale("korean", "south korea", "Korean / KR", "세계 정치 OR 인공지능 OR 기술 OR 과학 뉴스"),
    Locale("indonesian", "indonesia", "Indonesian / ID", "politik dunia OR kecerdasan buatan OR teknologi OR sains berita"),
    Locale("turkish", "turkey", "Turkish / TR", "dünya siyaseti OR yapay zeka OR teknoloji OR bilim haberler"),
)


def load_locales(path: Path | None = None) -> list[Locale]:
    """Operator locale list from ``config/locales.txt`` if present, else ``DEFAULT_LOCALES``.

    Line format: ``language | country | label | terms`` (``#`` comments, blanks ignored; country,
    label and terms all optional). When ``terms`` is omitted but the language matches a built-in
    locale, the built-in's native query is reused so the Apify floor still works.
    """
    if path and path.exists():
        builtin = {x.language: x.terms for x in DEFAULT_LOCALES}
        out: list[Locale] = []
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if parts and parts[0]:
                lang = parts[0]
                country = parts[1] if len(parts) > 1 else ""
                label = parts[2] if len(parts) > 2 and parts[2] else (f"{lang}/{country}" if country else lang)
                terms = parts[3] if len(parts) > 3 and parts[3] else builtin.get(lang, "")
                out.append(Locale(language=lang, country=country, label=label, terms=terms))
        if out:
            return out
    return list(DEFAULT_LOCALES)
