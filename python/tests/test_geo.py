"""#291 — maat/geo.py country-inference heuristics (moved out of serving.feed; curation-only)."""

from maat.geo import infer_country, source_country


def test_source_country_fr_tld():
    assert source_country("lemonde.fr") == "FR"


def test_source_country_co_uk():
    assert source_country("bbc.co.uk") == "GB"


def test_source_country_de():
    assert source_country("spiegel.de") == "DE"


def test_source_country_com_unknown():
    assert source_country("reuters.com") == ""


def test_source_country_empty_and_none():
    assert source_country("") == ""
    assert source_country(None) == ""  # type: ignore[arg-type]


def test_infer_country_from_tld():
    art_meta = {"a1": {"source": "lemonde.fr"}}
    assert infer_country([], art_meta, [["a1"]]) == "FR"


def test_infer_country_from_language():
    assert infer_country([{"language": "de"}], {}, [["unknown"]]) == "DE"


def test_infer_country_english_skipped():
    assert infer_country([{"language": "en"}], {}, []) == ""  # English doesn't narrow to a country


def test_infer_country_unknown():
    assert infer_country([], {}, []) == ""


def test_infer_country_parses_json_string_originators():
    # asyncpg may hand back originators as a JSON string — _as_list handles str + decoded.
    art_meta = {"a1": {"source": "bbc.co.uk"}}
    assert infer_country([], art_meta, '[["a1"]]') == "GB"
