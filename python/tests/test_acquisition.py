"""Acquisition funnel — pure tests (marketing site → console /acquisition). No DB, NATS or net.

Covers the event contract, the marketing helpers (ua family / platform / email), and the
operator page builder (KPIs, view→click conversion, the launch list, and graceful degrade).
"""

import datetime as dt

from maat import events
from maat.marketing.app import norm_platform, ua_family, valid_email
from maat.web.app import _acquisition_page, _nav


def test_event_types_registered():
    assert events.ACQUISITION_PAGE_VIEWED == "acquisition.page_viewed"
    assert events.ACQUISITION_CTA_CLICKED == "acquisition.cta_clicked"
    assert events.ACQUISITION_NOTIFY_REQUESTED == "acquisition.notify_requested"
    assert events.PUBLIC_TENANT == "public"  # pre-user, not a real tenant
    assert events.ACQUISITION_EVENT_TYPES == {
        "acquisition.page_viewed",
        "acquisition.cta_clicked",
        "acquisition.notify_requested",
    }


def test_nav_has_acquisition_tab():
    assert "Acquisition" in _nav("content")
    assert 'class="on"' in _nav("acquisition")  # active highlight on its own page


def test_ua_family_coarse_mapping():
    assert ua_family("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)") == "ios"
    assert ua_family("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)") == "mac"
    assert ua_family("Mozilla/5.0 (X11; Linux x86_64)") == "linux"
    assert ua_family("Mozilla/5.0 (Windows NT 10.0)") == "windows"
    assert ua_family("") == "other"


def test_norm_platform_defaults_to_ios():
    assert norm_platform("mac") == "mac"
    assert norm_platform("iOS") == "ios"
    assert norm_platform("garbage") == "ios"  # unknown -> App Store
    assert norm_platform("") == "ios"


def test_valid_email():
    assert valid_email("a@b.co")
    assert not valid_email("nope")
    assert not valid_email("a@b")  # needs a dotted domain
    assert not valid_email("")


def test_acquisition_page_kpis_conversion_and_launch_list():
    funnel = {"views": 200, "clicks": 50, "notifies": 12, "signups": 11}
    by_platform = [{"platform": "ios", "clicks": 40}, {"platform": "mac", "clicks": 10}]
    referrers = [
        {"referrer": "news.ycombinator.com", "clicks": 18},
        {"referrer": "direct", "clicks": 32},
    ]
    daily = [
        {"day": dt.date(2026, 6, 14), "views": 120, "clicks": 30},
        {"day": dt.date(2026, 6, 15), "views": 80, "clicks": 20},
    ]
    signups = [
        {"email": "reader@example.com", "platform": "ios",
         "first_seen": dt.datetime(2026, 6, 15, 9, 0), "hits": 2},
    ]
    out = _acquisition_page(funnel, by_platform, referrers, daily, signups)
    assert "Acquisition" in out
    assert "25%" in out  # 50/200 view->click conversion, not a raw count
    assert "iPhone · App Store" in out and "Mac" in out
    assert "news.ycombinator.com" in out
    assert "reader@example.com" in out
    assert "/acquisition/signups.csv" in out  # CSV export offered when there are sign-ups


def test_acquisition_page_handles_zero_and_missing_table():
    empty = _acquisition_page({"views": 0, "clicks": 0, "notifies": 0, "signups": 0}, [], [], [], [])
    assert "—" in empty  # conversion is a dash, never a divide-by-zero
    assert "No sign-ups yet" in empty
    assert "/acquisition/signups.csv" not in empty  # nothing to export -> no link

    degraded = _acquisition_page({}, [], [], [], [], ready=False)
    assert "restart the kernel" in degraded  # graceful note, not a 500
