"""The tick supervisor (#417) — the tests the shell loop could never have had.

The loop this replaces took prod down three ways, and each of those is asserted here as a property:
a hung step must be KILLED and the tick must continue (the old loop deadlocked forever); a failing
step must be RECORDED, not laundered to exit 0; a gated-off step must not run at all.
"""

from __future__ import annotations

import json
import sys


from scripts import clock_supervisor as cs


def _step(name: str, code: str, timeout_s: int = 30, **kw) -> cs.Step:
    """A step that runs a snippet of python — a real subprocess, so the timeout is really tested."""
    return cs.Step(name, [sys.executable, "-c", code], timeout_s, **kw)


def test_hung_step_is_killed_and_the_tick_continues(tmp_path, monkeypatch):
    # THE bug that stopped Maat: contradiction wedged for 6h+, so every later step (harvest) and
    # every FUTURE tick never ran — `|| echo` catches exits, not hangs. A hung step must now be
    # bounded and the steps after it must still run.
    monkeypatch.setattr(cs, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(cs, "HEARTBEAT_PATH", tmp_path / "beat")

    tick = cs.run_tick([
        _step("hangs", "import time; time.sleep(60)", timeout_s=1),
        _step("after-the-hang", "print('i still ran')"),
    ])

    outcomes = {s.name: s.outcome for s in tick.steps}
    assert outcomes["hangs"] == "timeout"          # killed, not waited on forever
    assert outcomes["after-the-hang"] == "ok"      # the tick was NOT deadlocked
    assert tick.finished                            # the tick completed at all


def test_failing_step_is_recorded_not_laundered(tmp_path, monkeypatch):
    # `|| echo "[clock] X error"` turned a 27-day outage into a log line nobody read. A failure must
    # be a recorded fact with its exit code and error tail.
    monkeypatch.setattr(cs, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(cs, "HEARTBEAT_PATH", tmp_path / "beat")

    tick = cs.run_tick([
        _step("boom", "import sys; sys.stderr.write('ArrayMemoryError: 30.1 GiB\\n'); sys.exit(1)"),
        _step("fine", "print('ok')"),
    ])

    boom = next(s for s in tick.steps if s.name == "boom")
    assert boom.outcome == "failed"
    assert boom.exit_code == 1
    assert "ArrayMemoryError" in boom.detail       # the reason survives, not just "error"
    assert [s.name for s in tick.failed] == ["boom"]
    assert next(s for s in tick.steps if s.name == "fine").outcome == "ok"


def test_gated_step_does_not_run(tmp_path, monkeypatch):
    # The engine + contradiction ship OFF. A gated step must be skipped without executing anything.
    monkeypatch.setattr(cs, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(cs, "HEARTBEAT_PATH", tmp_path / "beat")
    monkeypatch.delenv("MAAT_TEST_GATE", raising=False)

    marker = tmp_path / "should-not-exist"
    off = _step("gated-off", f"open({str(marker)!r}, 'w').write('ran')",
                gate="MAAT_TEST_GATE", gate_default="0")
    tick = cs.run_tick([off])

    assert tick.steps[0].outcome == "skipped"
    assert not marker.exists(), "a gated-off step executed anyway"

    monkeypatch.setenv("MAAT_TEST_GATE", "1")
    tick = cs.run_tick([off])
    assert tick.steps[0].outcome == "ok"
    assert marker.exists()


def test_paused_step_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(cs, "HEARTBEAT_PATH", tmp_path / "beat")
    monkeypatch.setenv("MAAT_INTAKE_PAUSED", "1")
    tick = cs.run_tick([_step("acquire", "print('x')", paused_by="MAAT_INTAKE_PAUSED")])
    assert tick.steps[0].outcome == "skipped"


def test_status_file_records_every_step(tmp_path, monkeypatch):
    # Liveness must be a FACT ON DISK, not inferred from the absence of a complaint. The healthcheck
    # and the watchdog read this; `[clock] tick done` alone could never say WHICH steps ran.
    status, beat = tmp_path / "status.json", tmp_path / "beat"
    monkeypatch.setattr(cs, "STATUS_PATH", status)
    monkeypatch.setattr(cs, "HEARTBEAT_PATH", beat)
    monkeypatch.delenv("MAAT_TEST_GATE", raising=False)

    cs.run_tick([
        _step("ok-step", "print('a')"),
        _step("bad-step", "import sys; sys.exit(2)"),
        _step("off-step", "print('b')", gate="MAAT_TEST_GATE", gate_default="0"),
    ])

    data = json.loads(status.read_text())
    assert {s["name"]: s["outcome"] for s in data["steps"]} == {
        "ok-step": "ok", "bad-step": "failed", "off-step": "skipped",
    }
    assert data["failed"] == ["bad-step"]
    assert data["ok"] == 1
    assert beat.read_text().strip(), "heartbeat not written — the healthcheck would go stale"


def test_the_engine_and_contradiction_ship_off_by_default(monkeypatch):
    # cauri's instruction: fix it, but leave the engine BUILT AND OFF. Assert the shipped defaults,
    # so nobody arms the engine by accident with an unrelated change.
    for var in ("MAAT_CORROBORATE_ENABLED", "MAAT_CONTRADICTION_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    by_name = {s.name: s for s in cs.STEPS}
    assert cs._skip_reason(by_name["corroborate"]) == "MAAT_CORROBORATE_ENABLED off"
    assert cs._skip_reason(by_name["contradiction"]) == "MAAT_CONTRADICTION_ENABLED off"
    # …and they arm with exactly one env var each.
    monkeypatch.setenv("MAAT_CORROBORATE_ENABLED", "1")
    assert cs._skip_reason(by_name["corroborate"]) is None


def test_every_step_has_a_hard_timeout():
    # The property that would have prevented the outage: NO step may be unbounded. Asserted over the
    # real STEPS table so a future step can't be added without one.
    assert cs.STEPS, "no steps configured"
    for s in cs.STEPS:
        assert s.timeout_s > 0, f"{s.name} has no timeout — this is exactly what deadlocked prod"
        assert s.timeout_s <= 2400, f"{s.name} timeout {s.timeout_s}s is longer than a tick's budget"


def test_harvest_runs_before_the_expensive_experimental_steps():
    # On 2026-07-14 a contradiction hang starved harvest and truth-over-time snapshots stopped for
    # 25h. Cheap + durable must come first, so an experimental step's failure can't erase history.
    order = [s.name for s in cs.STEPS]
    assert order.index("harvest") < order.index("contradiction")
    assert order.index("harvest") < order.index("grounding")
    assert order.index("harvest") < order.index("geotag")
