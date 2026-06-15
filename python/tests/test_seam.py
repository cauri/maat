"""Provider-seam tests — Mistral embeddings batching.

Regression for the clustering 400-at-scale bug: mistral_embed sent the whole corpus in one
request, which 400'd once the live feed grew past ~74 claims. It must chunk the inputs.
"""

from __future__ import annotations

from maat.providers import seam


class _FakeResp:
    def __init__(self, n: int):
        self._n = n

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
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
