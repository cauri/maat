"""Tests for the per-locale config (#239) — pure config loading + the balance of the default set."""

from __future__ import annotations

from maat.acquire import locales as loc


def test_default_locales_are_multipolar_and_under_represented_languages():
    ls = loc.load_locales(None)
    langs = {x.language for x in ls}
    # deliberately the languages an Anglophone-default stream under-covers — and NOT english
    assert {"arabic", "chinese", "russian", "spanish", "hindi", "japanese"} <= langs
    assert "english" not in langs
    assert len(ls) >= 10


def test_default_locales_all_carry_native_apify_terms():
    # the Apify floor (#239) needs an own-language query per locale, or it can't fill that locale
    ls = loc.load_locales(None)
    assert all(x.terms.strip() for x in ls), [x.label for x in ls if not x.terms.strip()]
    # terms are genuinely in-language, not the English label — spot-check a non-Latin script
    chinese = next(x for x in ls if x.language == "chinese")
    assert any("一" <= ch <= "鿿" for ch in chinese.terms)  # has CJK


def test_load_locales_reads_operator_file(tmp_path):
    cfg = tmp_path / "locales.txt"
    cfg.write_text(
        "# my locales\n"
        "arabic | | Arabic global\n"
        "\n"
        "korean | south korea | Korean / KR\n"
        "swahili|kenya\n"             # label optional -> derived
    )
    ls = loc.load_locales(cfg)
    assert [x.language for x in ls] == ["arabic", "korean", "swahili"]
    assert ls[0].country == "" and ls[0].label == "Arabic global"
    assert ls[1].country == "south korea"
    assert ls[2].label == "swahili/kenya"   # derived when omitted


def test_load_locales_terms_explicit_and_inherited(tmp_path):
    cfg = tmp_path / "locales.txt"
    cfg.write_text(
        "chinese | | Chinese | 自定义 查询\n"   # explicit 4th field -> used verbatim
        "arabic\n"                              # terms omitted -> inherit the built-in arabic query
        "swahili | kenya | Swahili\n"           # unknown language, no terms -> empty (Apify skips it)
    )
    ls = loc.load_locales(cfg)
    by_lang = {x.language: x for x in ls}
    assert by_lang["chinese"].terms == "自定义 查询"   # explicit terms used verbatim
    # arabic omitted its terms -> inherits the built-in arabic native query (non-empty)
    builtin_arabic = next(x.terms for x in loc.DEFAULT_LOCALES if x.language == "arabic")
    assert by_lang["arabic"].terms == builtin_arabic and builtin_arabic
    assert by_lang["swahili"].terms == ""            # unknown language, no terms -> Apify skips it
