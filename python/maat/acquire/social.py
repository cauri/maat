"""Social origin search (P16 #453) — where "I heard that…" claims usually start.

X and Reddit search via Apify actors, for the origin trace ONLY. The source-gate rule stands in
full: social posts are NEVER corroboration, never originators, never a score input — they feed
``OriginTrace`` display ("circulating on X since <date>") and nothing else. That separation is
structural: this module returns ``SocialPost``s, which no fold accepts.

Actor choice is ops-swappable (env), because third-party actors break: defaults were picked on
success rate + date-filter support (apidojo/twitter-scraper-lite 99.9%, harshmaur/reddit-scraper
96.5%). Parsing is defensive across actor output shapes — a missing field drops the post, never
the run; a failed leg returns [], logged.

PRIVACY GUARD (#453 → refined by #456): an author is NAMED only when they are plainly a public
account (verified, or follower count ≥ ``MAAT_SOCIAL_AUTHOR_MIN_FOLLOWERS``); anyone else
surfaces as "a social media account" — a reader-typed rumour must not put a small private
account's name on a public verdict page. Reddit authors are pseudonymous handles and follow the
same rule via subscriber counts where exposed (usually not → anonymised).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("maat.acquire.social")

_RUN_SYNC = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

X_ACTOR = os.environ.get("MAAT_SOCIAL_X_ACTOR", "apidojo~twitter-scraper-lite")
REDDIT_ACTOR = os.environ.get("MAAT_SOCIAL_REDDIT_ACTOR", "harshmaur~reddit-scraper")
_MIN_FOLLOWERS = int(os.environ.get("MAAT_SOCIAL_AUTHOR_MIN_FOLLOWERS", "10000"))
_TIMEOUT = float(os.environ.get("MAAT_SOCIAL_TIMEOUT", "90"))


def available() -> bool:
    return bool(os.environ.get("APIFY_API_KEY"))


@dataclass(frozen=True)
class SocialPost:
    """One social post that may be an origin-trace candidate — display data only, by design."""

    platform: str        # "x" | "reddit"
    author: str          # handle ("@name" / "u/name") — shown ONLY when author_public
    author_public: bool  # verified or above the follower floor → safe to name
    text: str
    url: str             # permalink
    created_at: str      # ISO 8601 ("" unknown → the post can't be an earliest candidate)
    engagement: int      # likes+reposts / score — the spread signal


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _iso(v: Any) -> str:
    """Actor timestamps arrive as ISO strings, epoch seconds, or X's ctime — normalise or ''."""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:  # X classic: "Wed Jul 10 08:00:00 +0000 2026"
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return ""


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_x_items(items: list) -> list[SocialPost]:
    out: list[SocialPost] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        author = it.get("author") if isinstance(it.get("author"), dict) else {}
        handle = _first(author, "userName", "username", "screen_name") or _first(
            it, "userName", "username")
        text = _first(it, "text", "fullText", "full_text")
        url = _first(it, "url", "twitterUrl", "tweetUrl", "permalink")
        if not handle or not text or not url:
            continue
        followers = _int(_first(author, "followers", "followersCount", "followers_count"))
        verified = bool(_first(author, "isVerified", "verified", "isBlueVerified"))
        out.append(SocialPost(
            platform="x",
            author=f"@{str(handle).lstrip('@')}",
            author_public=verified or followers >= _MIN_FOLLOWERS,
            text=str(text),
            url=str(url),
            created_at=_iso(_first(it, "createdAt", "created_at", "timestamp", "time")),
            engagement=_int(_first(it, "likeCount", "likes", "favorite_count"))
            + _int(_first(it, "retweetCount", "retweets", "retweet_count")),
        ))
    return out


def parse_reddit_items(items: list) -> list[SocialPost]:
    out: list[SocialPost] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        author = _first(it, "author", "username", "userName")
        text = " ".join(str(p) for p in (_first(it, "title"), _first(it, "body", "text", "selftext"))
                        if p) or None
        url = _first(it, "url", "postUrl", "permalink", "link")
        if not author or not text or not url:
            continue
        if str(url).startswith("/r/"):
            url = f"https://www.reddit.com{url}"
        subscribers = _int(_first(it, "subscribers", "communitySubscribers"))
        out.append(SocialPost(
            platform="reddit",
            author=f"u/{str(author).removeprefix('u/')}",
            author_public=subscribers >= _MIN_FOLLOWERS,  # usually absent → anonymised
            text=str(text),
            url=str(url),
            created_at=_iso(_first(it, "createdAt", "created_utc", "created", "postedAt")),
            engagement=_int(_first(it, "score", "upVotes", "upvotes", "numberOfVotes")),
        ))
    return out


def _run(actor: str, payload: dict, *, timeout: float) -> list:
    token = os.environ.get("APIFY_API_KEY")
    if not token:
        return []
    r = httpx.post(
        _RUN_SYNC.format(actor=actor), params={"token": token}, json=payload, timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def search_x(query: str, *, max_items: int = 20, timeout: float = _TIMEOUT) -> list[SocialPost]:
    """X posts matching ``query`` — best-effort ([] on any failure, logged)."""
    try:
        items = _run(X_ACTOR, {
            "searchTerms": [query], "maxItems": max_items, "sort": "Top",
        }, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - a dead leg degrades the trace, never the run
        log.warning("x search failed for %r: %s", query[:60], type(e).__name__)
        return []
    return parse_x_items(items)


def search_reddit(query: str, *, max_items: int = 20, timeout: float = _TIMEOUT) -> list[SocialPost]:
    """Reddit posts matching ``query`` — best-effort ([] on any failure, logged)."""
    try:
        items = _run(REDDIT_ACTOR, {
            "searchTerms": [query], "searchPosts": True, "searchComments": False,
            "maxPostsCount": max_items, "searchSort": "relevance", "crawlCommentsPerPost": False,
        }, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("reddit search failed for %r: %s", query[:60], type(e).__name__)
        return []
    return parse_reddit_items(items)
