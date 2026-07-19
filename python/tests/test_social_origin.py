"""Tests for the social origin search (P16 #453) — parsing, anonymisation, failure paths.

The rule under test: social is DISPLAY-ONLY provenance. Authors are named only when plainly
public (verified / above the follower floor); everyone else is "a social media account" — a
typed rumour must never put a small private account's name on a public verdict page.
"""

from __future__ import annotations

from maat.acquire import social
from maat.acquire.social import parse_reddit_items, parse_x_items, search_x
from maat.pipeline.origin import _social_label


def test_parse_x_items_defensive_and_anonymisation_flag():
    items = [
        {"author": {"userName": "TechLeaks", "followers": 250_000, "isVerified": False},
         "text": "BREAKING: SpaceX and Apple to merge", "url": "https://x.com/TechLeaks/1",
         "createdAt": "2026-07-09T08:00:00Z", "likeCount": 1200, "retweetCount": 400},
        {"author": {"userName": "smallacct", "followers": 12, "isVerified": False},
         "text": "heard spacex apple merging??", "url": "https://x.com/smallacct/2",
         "createdAt": "Wed Jul 08 08:00:00 +0000 2026", "likeCount": 1},
        {"text": "no author", "url": "https://x.com/x/3"},                 # dropped
        {"author": {"userName": "nourl"}, "text": "t"},                    # dropped
        "not a dict",                                                      # dropped
    ]
    got = parse_x_items(items)
    assert len(got) == 2
    big, small = got
    assert big.author == "@TechLeaks" and big.author_public is True
    assert big.engagement == 1600
    assert big.created_at.startswith("2026-07-09")
    assert small.author_public is False
    assert small.created_at.startswith("2026-07-08")   # X ctime format normalised


def test_parse_reddit_items_relative_permalink_and_bad_date():
    items = [
        {"author": "rumourfan", "title": "SpaceX Apple merger?", "body": "is this real",
         "permalink": "/r/space/comments/abc", "created_utc": "not-a-time", "score": 40},
    ]
    got = parse_reddit_items(items)
    assert len(got) == 1
    post = got[0]
    assert post.author == "u/rumourfan"
    assert post.author_public is False                 # no subscriber signal → anonymised
    assert post.url == "https://www.reddit.com/r/space/comments/abc"
    assert post.created_at == ""                       # undated → never an earliest candidate
    assert post.engagement == 40


def test_parse_reddit_epoch_seconds_normalise():
    got = parse_reddit_items([
        {"author": "a", "title": "SpaceX Apple merge", "url": "https://redd.it/x",
         "created_utc": 1783500000, "score": 3},
    ])
    assert got[0].created_at.startswith("2026-07-08")


def test_social_label_anonymises_private_authors():
    named = parse_x_items([{
        "author": {"userName": "BigDesk", "isVerified": True},
        "text": "t", "url": "https://x.com/BigDesk/1",
    }])[0]
    anon = parse_x_items([{
        "author": {"userName": "tiny", "followers": 3},
        "text": "t", "url": "https://x.com/tiny/1",
    }])[0]
    assert _social_label(named) == "@BigDesk on X"
    assert _social_label(anon) == "a social media account on X"


def test_search_x_without_key_or_on_failure_returns_empty(monkeypatch):
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    assert search_x("anything") == []

    monkeypatch.setenv("APIFY_API_KEY", "k")

    def boom(*_a, **_k):
        raise OSError("actor down")

    monkeypatch.setattr(social.httpx, "post", boom)
    assert search_x("anything") == []
