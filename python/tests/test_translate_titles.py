"""Feed title translation (#54) — the English gloss on the card + the step's language gate."""

from __future__ import annotations

from maat.agents.translate_titles import _is_english
from maat.web.app import _card


def test_card_shows_english_gloss_next_to_nonenglish_title():
    a = {"source": "Le Monde", "title": "Élection en France", "url": "https://lemonde.fr/x", "language": "fr"}
    out = _card(a, [], "Election in France")
    assert "Élection en France" in out   # original kept
    assert "Election in France" in out    # English gloss shown beside it
    assert 'target="_blank"' in out       # the title still links out in a new tab


def test_card_no_gloss_when_translation_equals_original():
    out = _card({"source": "X", "title": "Same", "url": "", "language": "fr"}, [], "Same")
    assert out.count("Same") == 1         # not shown twice


def test_card_no_gloss_for_english_or_missing_translation():
    out = _card({"source": "BBC", "title": "English headline", "url": "https://bbc.com", "language": "en"}, [])
    assert "title-en" not in out


def test_is_english_gate():
    assert _is_english("en") and _is_english("EN") and _is_english("en-GB") and _is_english("")
    assert not _is_english("fr") and not _is_english("zh") and not _is_english("es")
