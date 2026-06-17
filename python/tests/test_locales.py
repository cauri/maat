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
