"""GDELT acquisition tests — pure param-building + response-parsing (no network)."""

from maat.acquire.gdelt import build_params, parse_articles


def test_build_params_adds_filters_and_modes():
    p = build_params("ecb rate", sourcelang="French", sourcecountry="France", maxrecords=5)
    assert "ecb rate" in p["query"]
    assert "sourcelang:French" in p["query"]
    assert "sourcecountry:France" in p["query"]
    assert p["mode"] == "artlist"
    assert p["format"] == "json"
    assert p["maxrecords"] == "5"


def test_parse_articles_maps_fields_and_skips_urlless():
    data = {
        "articles": [
            {
                "url": "https://a.example/1",
                "title": "  Headline  ",
                "domain": "a.example",
                "language": "Spanish",
                "sourcecountry": "Spain",
                "seendate": "20260611T120000Z",
            },
            {"title": "no url here"},  # must be skipped
        ]
    }
    arts = parse_articles(data)
    assert len(arts) == 1
    assert arts[0].url == "https://a.example/1"
    assert arts[0].title == "Headline"  # stripped
    assert arts[0].language == "Spanish"
    assert arts[0].country == "Spain"


def test_parse_articles_empty_is_safe():
    assert parse_articles({}) == []
    assert parse_articles({"articles": None}) == []
