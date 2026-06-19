"""Shared pytest fixtures."""

import pytest

from maat.providers import seam


@pytest.fixture(autouse=True)
def _disable_seam_throttle(monkeypatch):
    """Neutralise the provider-seam rate throttle (#300) for the whole suite.

    The module-level limiters real-``time.sleep`` once their bucket drains; integration tests that
    exercise the real seam with a faked transport would otherwise pace at the configured RPM and
    blow the suite's wall-clock. The throttle's own logic (and the router's selection) are covered
    directly in test_seam.py via locally-constructed buckets/routers, which this does not touch.
    Prod is unaffected (no conftest there).
    """
    monkeypatch.setattr(seam, "_MISTRAL_LIMIT", seam._RateLimiter(0, 0))
    # Claude calls go through the router now; zero every endpoint's limiter (covers _CLAUDE_LIMIT).
    monkeypatch.setattr(seam, "_CLAUDE_ROUTER", seam._CLAUDE_ROUTER.with_zero_limits())
