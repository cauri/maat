"""Ingestion text hygiene (#33) — clean_title / clean_body against the real BBC-via-Apify junk."""

from __future__ import annotations

from maat.acquire.clean import clean_body, clean_title, is_index_page


# --- titles -----------------------------------------------------------------------------

def test_strips_trailing_publisher_matching_source():
    # the exact case from the iOS article page
    assert clean_title("Happy News: Stories to make you smile - BBC Newsround", "bbc.co.uk") == \
        "Happy News: Stories to make you smile"


def test_strips_trailing_publisher_by_publication_word_when_domain_differs():
    # nytimes.com domain token won't match "The New York Times", but "Times" is a publication word
    assert clean_title("Trump signs the order - The New York Times", "nytimes.com") == \
        "Trump signs the order"
    assert clean_title("Markets rally as inflation cools | The Guardian", "theguardian.com") == \
        "Markets rally as inflation cools"


def test_keeps_the_colon_headline_device():
    assert clean_title("Happy News: Stories to make you smile", "bbc.co.uk") == \
        "Happy News: Stories to make you smile"


def test_does_not_clip_a_real_dash_headline():
    # no publisher match, no publication word -> left alone
    assert clean_title("Apple unveils a thinner laptop - hands on first impressions", "example.com") == \
        "Apple unveils a thinner laptop - hands on first impressions"


def test_strips_leading_publisher_on_brand_match():
    assert clean_title("BBC: Floods displace thousands in the region", "bbc.co.uk") == \
        "Floods displace thousands in the region"


# --- bodies -----------------------------------------------------------------------------

# Reconstructed verbatim from the two screenshots of the bbc.co.uk Newsround article.
DIRTY_BBC = """\
Happy News: Stories to make you smile - BBC Newsround
[![Link to newsround](https://static.files.bbci.co.uk/core/website/assets/static/childrens-web/cbbc-product-navigation/newsround-branding.58fb72a69d.svg)](/newsround)
# Happy News: Stories to make you smile
Happy News: Stories to make you smileClose
Happy News. We've got a pretty cool dragon boat festival, the cutest baby hippo to show you, and we meet the fashion designer who is only 10.
Watch our other weekly catch-ups:
[Strange News](/newsround/videos/czdd1p9ejxno)
[Your Planet](/newsround/videos/cn007drpeq5o)
*   Published
1 day ago
Share
close panel
Share page
Copy link
[About sharing](https://www.bbc.co.uk/usingthebbc/terms/can-i-share-things-from-the-bbc)
Read description
"""


def test_clean_body_strips_the_real_apify_markdown_junk():
    out = clean_body(DIRTY_BBC, title="Happy News: Stories to make you smile")

    # the real article prose survives
    assert "dragon boat festival" in out
    assert "fashion designer who is only 10" in out

    # every flavour of junk is gone
    assert "![" not in out and "](" not in out          # no markdown image/link syntax
    assert "https://" not in out and "http://" not in out  # no bare URLs
    assert "#" not in out                                 # no heading markers
    assert "/newsround" not in out                        # no relative nav targets
    assert "Strange News" not in out                      # nav-link labels dropped
    assert "Your Planet" not in out
    for chrome in ("close panel", "Share page", "Copy link", "Read description", "About sharing"):
        assert chrome not in out
    assert "1 day ago" not in out                         # timestamp line
    assert "smileClose" not in out                        # glued nav token on the dup headline
    # the headline isn't repeated as a body line
    assert out.count("Happy News: Stories to make you smile") == 0


def test_clean_body_is_idempotent():
    once = clean_body(DIRTY_BBC, title="Happy News: Stories to make you smile")
    assert clean_body(once, title="Happy News: Stories to make you smile") == once


def test_clean_body_leaves_trafilatura_prose_untouched():
    prose = (
        "The central bank held rates steady on Thursday.\n\n"
        "Officials cited easing inflation and a resilient labour market as reasons to wait."
    )
    assert clean_body(prose) == prose


def test_clean_body_strips_dangling_link_tails():
    # the wired.com case: [label] and ](url) split across lines by markdown line-wrap
    body = (
        "My Father Wants to Age in Place. AI Will Be Watching\n"
        "](/story/sensi-ai-seniors-home-care-aging-in-place/)\n"
        "By Steven Blum"
    )
    out = clean_body(body)
    assert "](" not in out and "/story/" not in out
    assert "My Father Wants to Age in Place" in out   # the link label (prose) is kept


# --- index / section pages --------------------------------------------------------------

def test_index_page_detected_by_section_title():
    assert is_index_page("Artificial Intelligence | Latest News, Photos & Videos", "")
    assert is_index_page("Climate — News & Analysis", "")
    assert is_index_page("Tech News Archives", "")


def test_real_article_titles_are_not_index_pages():
    assert not is_index_page("Fed holds rates steady as inflation cools", "Some real prose.")
    assert not is_index_page("Latest news on the merger talks emerges", "")   # mid-headline, no delimiter
    assert not is_index_page("Happy News: Stories to make you smile - BBC Newsround", "")


def test_index_page_detected_by_link_dense_body():
    body = "\n".join(f"[Headline number {i}](/story/{i})" for i in range(12))
    assert is_index_page("Some Section", body)


def test_prose_body_is_not_an_index_page():
    body = "\n".join(
        "The central bank held rates steady on Thursday, citing easing inflation pressures."
        for _ in range(12)
    )
    assert not is_index_page("A real article", body)
