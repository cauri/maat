"""#58 — feedback intake. The POST /api/feedback route calls feedback.record(pool, None, ...),
which direct-writes a feedback.submitted event the triage agent then routes. Tests that path."""

import asyncio

from maat.serving import feedback


class _FakePool:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))


def test_record_writes_feedback_submitted_event():
    pool = _FakePool()
    fid = asyncio.run(
        feedback.record(pool, None, text="wrong verdict on the Reyes story", source="reader")
    )
    assert fid  # an item_id is returned to the client
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "insert into events" in query
    assert args[1] == feedback.FEEDBACK_SUBMITTED  # event type
    assert "wrong verdict" in args[2]  # data json carries the feedback text


def test_record_uses_supplied_item_id():
    pool = _FakePool()
    fid = asyncio.run(feedback.record(pool, None, item_id="fb-123", text="x", source="reader"))
    assert fid == "fb-123"
