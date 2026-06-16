"""#189 — DRAFT LLM curation geo-tagging.

Two seams:
  * maat.pipeline.geotag.llm_country  — the bulk-model call (mocked), ISO-2 validated.
  * maat.serving.feed._resolve_country — heuristic-first precedence: the LLM override only fills
    the gap the TLD/language heuristic left blank, and never overrides a heuristic hit.
"""

import maat.pipeline.geotag as geo
from maat.serving.feed import _resolve_country


class _Reply:
    def __init__(self, text):
        self.text = text


# --- pure LLM call -------------------------------------------------------------------------
# Patch the name bound in the geotag module (it imports claude_complete at module load), so the
# mock is actually exercised rather than the call erroring out to "" for the wrong reason.


def test_llm_country_parses_iso2(monkeypatch):
    monkeypatch.setattr(geo, "claude_complete", lambda *a, **k: _Reply('{"country": "ng"}'))
    assert geo.llm_country("Lagos floods displace thousands") == "NG"


def test_llm_country_global_is_blank(monkeypatch):
    monkeypatch.setattr(geo, "claude_complete", lambda *a, **k: _Reply('{"country": "XX"}'))
    assert geo.llm_country("A worldwide climate accord is signed") == ""


def test_llm_country_rejects_non_iso2(monkeypatch):
    monkeypatch.setattr(geo, "claude_complete", lambda *a, **k: _Reply('{"country": "Nigeria"}'))
    assert geo.llm_country("x") == ""


def test_llm_country_swallows_bad_output(monkeypatch):
    monkeypatch.setattr(geo, "claude_complete", lambda *a, **k: _Reply("not json"))
    assert geo.llm_country("x") == ""


def test_llm_country_empty_text_no_call(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not call the model for empty text")

    monkeypatch.setattr(geo, "claude_complete", _boom)
    assert geo.llm_country("   ") == ""


# --- heuristic-first precedence ------------------------------------------------------------

# A source with a recognised TLD (.fr → FR) so the heuristic places it without the LLM.
_FR_META = {"a1": {"source": "lemonde.fr", "language": "fr"}}
_FR_CLAIMS = [{"article_id": "a1", "language": "fr"}]
_FR_ORIG = [["a1"]]

# A bare-.com English source the heuristic can't place → country falls to the override.
_UNK_META = {"a2": {"source": "example.com", "language": "en"}}
_UNK_CLAIMS = [{"article_id": "a2", "language": "en"}]
_UNK_ORIG = [["a2"]]


def test_resolve_uses_heuristic_when_it_places():
    # Heuristic resolves .fr → FR; the override is ignored even though it disagrees.
    out = _resolve_country(_FR_CLAIMS, _FR_META, _FR_ORIG, "c1", {"c1": "US"})
    assert out == "FR"


def test_resolve_falls_back_to_override_for_gap():
    out = _resolve_country(_UNK_CLAIMS, _UNK_META, _UNK_ORIG, "c2", {"c2": "NG"})
    assert out == "NG"


def test_resolve_blank_when_no_override():
    assert _resolve_country(_UNK_CLAIMS, _UNK_META, _UNK_ORIG, "c2", None) == ""
    assert _resolve_country(_UNK_CLAIMS, _UNK_META, _UNK_ORIG, "c2", {"other": "NG"}) == ""
