"""Provider-seam tests — Mistral embeddings batching + transient-failure retry.

Two regressions:
  * Batching: mistral_embed sent the whole corpus in one request, which 400'd once the live feed
    grew past ~74 claims. It must chunk the inputs.
  * Retry: a single un-retried 429 (Mistral rate-limits under the pipeline's bursty load) crashed
    a whole corroborate run. Transient statuses must back off and retry; non-retryable ones must
    still fail fast.
"""

from __future__ import annotations

import httpx
import pytest

from maat.providers import seam


class _FakeResp:
    def __init__(self, n: int = 0, *, status_code: int = 200, headers: dict | None = None,
                 payload: dict | None = None):
        self._n = n
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self.request = httpx.Request("POST", "https://example.test")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=self.request, response=self  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        if self._payload is not None:
            return self._payload
        # One small embedding per input; usage echoes the chunk size.
        return {
            "data": [{"embedding": [float(i), 0.0, 0.0]} for i in range(self._n)],
            "usage": {"prompt_tokens": self._n},
        }


def test_mistral_embed_batches_large_input(monkeypatch):
    """130 inputs are chunked into 64/64/2 requests and concatenated in order."""
    calls: list[int] = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        calls.append(len(json["input"]))
        return _FakeResp(len(json["input"]))

    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setattr(seam.httpx, "post", fake_post)
    monkeypatch.setattr(seam, "_MISTRAL_MIN_INTERVAL", 0)  # don't real-sleep in the batching test

    out = seam.mistral_embed([f"claim {i}" for i in range(130)])

    assert len(out) == 130
    assert calls == [64, 64, 2]  # _EMBED_BATCH = 64, no single over-limit request
    assert all(len(vec) == 3 for vec in out)


def test_mistral_embed_empty_makes_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the API for empty input")

    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setattr(seam.httpx, "post", boom)

    assert seam.mistral_embed([]) == []


def test_post_json_retries_on_429_then_succeeds(monkeypatch):
    statuses = [429, 503, 200]
    calls = {"n": 0}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        s = statuses[calls["n"]]
        calls["n"] += 1
        return _FakeResp(status_code=s, headers={}, payload={"ok": True})

    monkeypatch.setattr(seam.httpx, "post", fake_post)
    monkeypatch.setattr(seam.time, "sleep", lambda *_: None)  # don't actually wait

    out = seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert out == {"ok": True}
    assert calls["n"] == 3  # 429 → 503 → 200


def test_post_json_raises_after_exhausting_retries(monkeypatch):
    calls = {"n": 0}

    def always_429(*a, **k):
        calls["n"] += 1
        return _FakeResp(status_code=429, headers={})

    monkeypatch.setattr(seam.httpx, "post", always_429)
    monkeypatch.setattr(seam.time, "sleep", lambda *_: None)
    monkeypatch.setattr(seam, "_MAX_RETRIES", 2)

    with pytest.raises(httpx.HTTPStatusError):
        seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert calls["n"] == 3  # initial + 2 retries


def test_post_json_does_not_retry_client_errors(monkeypatch):
    calls = {"n": 0}

    def bad_request(*a, **k):
        calls["n"] += 1
        return _FakeResp(status_code=400, headers={})

    monkeypatch.setattr(seam.httpx, "post", bad_request)
    monkeypatch.setattr(seam.time, "sleep", lambda *_: None)

    with pytest.raises(httpx.HTTPStatusError):
        seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert calls["n"] == 1  # 400 is not transient — fail fast, no retry


def test_post_json_honors_retry_after_header(monkeypatch):
    slept: list[float] = []
    statuses = [429, 200]
    calls = {"n": 0}

    def fake_post(*a, **k):
        s = statuses[calls["n"]]
        calls["n"] += 1
        return _FakeResp(status_code=s, headers={"retry-after": "7"}, payload={"ok": 1})

    monkeypatch.setattr(seam.httpx, "post", fake_post)
    monkeypatch.setattr(seam.time, "sleep", lambda d: slept.append(d))

    seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert slept == [7.0]  # server-directed delay used verbatim


def test_mistral_pace_spaces_calls_under_the_rate_limit(monkeypatch):
    # Deterministic clock: monotonic advances only when we "sleep". Proves successive Mistral calls
    # are forced ≥ _MISTRAL_MIN_INTERVAL apart (so a burst stays under Mistral's 60/min budget).
    clock = {"t": 100.0}
    slept: list[float] = []
    monkeypatch.setattr(seam.time, "monotonic", lambda: clock["t"])

    def fake_sleep(d):
        slept.append(d)
        clock["t"] += d  # time only moves forward by what we sleep

    monkeypatch.setattr(seam.time, "sleep", fake_sleep)
    monkeypatch.setattr(seam, "_MISTRAL_MIN_INTERVAL", 1.2)
    monkeypatch.setattr(seam, "_mistral_next", [0.0])

    for _ in range(4):
        seam._mistral_pace()
    # first call: no wait (budget free); next three each wait the full interval
    assert slept == pytest.approx([1.2, 1.2, 1.2])


def test_mistral_pace_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setattr(seam, "_MISTRAL_MIN_INTERVAL", 0)
    monkeypatch.setattr(seam.time, "sleep", lambda *_: (_ for _ in ()).throw(AssertionError("no sleep")))
    seam._mistral_pace()  # returns immediately, never sleeps
