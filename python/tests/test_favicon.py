"""Outlet favicons shared by the console (/source-icon) and the app (/api/v2/source-icon), #sources.
Pure + offline: the monogram fallback and validation never touch the network."""

import asyncio

from maat.serving.favicon import icon_bytes, monogram, valid_domain


def test_valid_domain_guards_the_fetch_url():
    assert valid_domain("reuters.com") and valid_domain("www.bbc.co.uk")
    assert not valid_domain("") and not valid_domain("nodot")
    assert not valid_domain("a/b.com") and not valid_domain("ev il.com")  # no path/space (no SSRF)


def test_monogram_is_a_deterministic_lettered_svg():
    svg = monogram("reuters.com")
    assert svg.startswith(b"<svg") and b">R<" in svg
    assert monogram("reuters.com") == svg                 # same outlet → same chip
    assert monogram("bbc.com") != svg                     # different outlet → different chip
    assert monogram("www.lemonde.fr").count(b">L<") == 1   # www. stripped before the letter


def test_icon_bytes_falls_back_to_monogram_offline():
    body, ctype = asyncio.run(icon_bytes("not-a-real-domain"))  # invalid → no fetch, straight to chip
    assert ctype == "image/svg+xml" and body.startswith(b"<svg")


def test_public_source_icon_endpoint_is_registered_under_api_v2():
    from maat.serving.feed import feed_router

    assert any(r.path.endswith("/source-icon") for r in feed_router.routes)
