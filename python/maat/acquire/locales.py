"""Per-locale acquisition config (#239) — actively pull under-represented languages/regions.

The default GDELT/RSS passes surface whatever ranks; this adds a deliberate FLOOR: for each
configured locale we query GDELT filtered to that language (and optionally country), so the
corpus carries Arabic, Chinese, Russian, Hindi, … coverage in its own language instead of only
what bubbles up through English. GDELT's ``sourcelang`` / ``sourcecountry`` take lowercase
NAMES (verified live: ``spanish`` / ``spain`` / ``russian`` / ``russia``), and it returns real
article URLs (unlike Google News, whose RSS links hit a consent wall).

Operator-editable via ``config/locales.txt`` (``language | country | label``, ``#`` comments;
blank country = that language globally).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Locale:
    language: str   # GDELT sourcelang NAME, e.g. "spanish"
    country: str    # GDELT sourcecountry NAME, e.g. "spain" ("" = that language globally)
    label: str


# Deliberately weighted toward languages the Anglophone-default stream under-covers.
DEFAULT_LOCALES: tuple[Locale, ...] = (
    Locale("arabic", "", "Arabic"),
    Locale("chinese", "", "Chinese"),
    Locale("russian", "russia", "Russian / RU"),
    Locale("spanish", "", "Spanish"),
    Locale("hindi", "india", "Hindi / IN"),
    Locale("portuguese", "brazil", "Portuguese / BR"),
    Locale("french", "", "French"),
    Locale("german", "germany", "German / DE"),
    Locale("japanese", "japan", "Japanese / JP"),
    Locale("korean", "south korea", "Korean / KR"),
    Locale("indonesian", "indonesia", "Indonesian / ID"),
    Locale("turkish", "turkey", "Turkish / TR"),
)


def load_locales(path: Path | None = None) -> list[Locale]:
    """Operator locale list from ``config/locales.txt`` if present, else ``DEFAULT_LOCALES``.

    Line format: ``language | country | label`` (``#`` comments, blanks ignored; country optional).
    """
    if path and path.exists():
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
                out.append(Locale(language=lang, country=country, label=label))
        if out:
            return out
    return list(DEFAULT_LOCALES)
