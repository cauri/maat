"""#158 — streaming console chat. Covers the NDJSON framing + graceful degradation of the
/prompts/chat/stream backend (seam.claude_stream is mocked; no network)."""

import asyncio
import json

import maat.providers.seam as seam
import maat.web.app as app


def _drain(agen):
    async def _run():
        return [chunk async for chunk in agen]

    return asyncio.run(_run())


def test_ndjson_frames_each_delta_then_done(monkeypatch):
    async def _fake(prompt, *, model=None, max_tokens=1024):
        for tok in ["Hel", "lo ", "world"]:
            yield tok

    monkeypatch.setattr(seam, "claude_stream", _fake)
    lines = [json.loads(line) for line in _drain(app._chat_ndjson("x", max_tokens=10))]
    assert lines == [{"t": "Hel"}, {"t": "lo "}, {"t": "world"}, {"done": True}]


def test_ndjson_preserves_newlines_in_one_line(monkeypatch):
    async def _fake(prompt, *, model=None, max_tokens=1024):
        yield "line1\nline2"

    monkeypatch.setattr(seam, "claude_stream", _fake)
    raw = _drain(app._chat_ndjson("x", max_tokens=10))
    # The delta has a newline, but the NDJSON line must not — it's JSON-escaped, so one record.
    assert raw[0].count("\n") == 1 and raw[0].rstrip("\n").count("\n") == 0
    assert json.loads(raw[0]) == {"t": "line1\nline2"}


def test_ndjson_provider_error_is_one_error_line(monkeypatch):
    async def _boom(prompt, *, model=None, max_tokens=1024):
        raise RuntimeError("provider down")
        yield  # unreachable, but the textual yield makes this an async generator

    monkeypatch.setattr(seam, "claude_stream", _boom)
    lines = _drain(app._chat_ndjson("x", max_tokens=10))
    assert len(lines) == 1
    assert json.loads(lines[0])["error"].startswith("Chat unavailable")


def test_ndjson_missing_key_is_actionable(monkeypatch):
    async def _nokey(prompt, *, model=None, max_tokens=1024):
        raise KeyError("ANTHROPIC_API_KEY")
        yield

    monkeypatch.setattr(seam, "claude_stream", _nokey)
    msg = json.loads(_drain(app._chat_ndjson("x", max_tokens=10))[0])["error"]
    assert "ANTHROPIC_API_KEY" in msg


def test_stream_endpoint_guards_non_editable_keys():
    req = app.PromptChatReq(key="definitely-not-a-real-key", current="", messages=[])

    async def _run():
        resp = await app.prompts_chat_stream(req)
        return [c async for c in resp.body_iterator]

    body = "".join(c.decode() if isinstance(c, bytes) else c for c in asyncio.run(_run()))
    assert "editable prompts only" in body
