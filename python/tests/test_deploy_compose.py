"""#419 — the deploy contract the watchdog depends on, asserted against the real compose file.

The watchdog can only tell "broken" from "switched off" if it can SEE the switches. It runs in its
own container (deliberately — the clock is the thing that fails, so it must not be able to silence
its own alarm), and a container gets no environment it isn't given. On the live box it was given
neither gate, so it alerted CRITICAL on four deliberately-paused stages every 15 minutes, forever.

Worse than a missing variable is a DISAGREEING one: if the clock defaults intake to paused and the
watchdog defaults it to running, the watchdog is describing a machine that does not exist — and it
would either alert forever or, in the other direction, stay quiet through a real outage. Nothing in
python's test suite reads the compose file, so nothing could catch that drift. This does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.prod.yml"

# The gates that MUST mean the same thing to the clock (which acts on them) and the watchdog (which
# reports on them). Drift between the two is silent and undetectable at runtime.
SHARED_GATES = ("MAAT_INTAKE_PAUSED", "MAAT_CORROBORATE_ENABLED")


def _service_block(text: str, name: str) -> str:
    """The YAML block for one top-level service (2-space indented key → next 2-space key)."""
    m = re.search(rf"^  {re.escape(name)}:$(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert m, f"service {name!r} not found in {COMPOSE}"
    return m.group(1)


def _default_of(block: str, var: str) -> str | None:
    """The `${VAR:-default}` default for `var` inside a service block, or None if unset."""
    m = re.search(rf"^\s*{re.escape(var)}:\s*\$\{{{re.escape(var)}:-([^}}]*)\}}", block, re.M)
    return m.group(1) if m else None


@pytest.fixture(scope="module")
def compose() -> str:
    return COMPOSE.read_text()


def test_watchdog_can_see_the_gates_it_reports_on(compose):
    """Without these the watchdog cannot distinguish a paused stage from a dead one."""
    block = _service_block(compose, "watchdog")
    for gate in SHARED_GATES:
        assert _default_of(block, gate) is not None, (
            f"watchdog has no {gate} — it cannot tell 'switched off' from 'broken', so it alerts "
            "CRITICAL on the intended state every 15 minutes, forever"
        )


def test_clock_and_watchdog_agree_on_every_shared_gate(compose):
    """The clock ACTS on these; the watchdog REPORTS on them. Disagreement is invisible at runtime."""
    clock = _service_block(compose, "acquisition-clock")
    watchdog = _service_block(compose, "watchdog")
    for gate in SHARED_GATES:
        assert _default_of(clock, gate) == _default_of(watchdog, gate), (
            f"{gate} default differs between acquisition-clock and watchdog — the watchdog would be "
            "describing a machine that does not exist"
        )


def test_the_engine_gates_are_off_by_default(compose):
    """cauri: "Leave it built but off." The default must be off — arming is an explicit act."""
    clock = _service_block(compose, "acquisition-clock")
    assert _default_of(clock, "MAAT_CORROBORATE_ENABLED") == "0"
    assert _default_of(clock, "MAAT_CONTRADICTION_ENABLED") == "0"


def test_every_clock_step_has_a_timeout():
    """#417's core lesson, pinned where it can't rot.

    A step with no timeout is what turned one hung agent into a 27-day outage: the contradiction
    hang deadlocked every later step AND every subsequent tick, so corroborate was never even
    retried. Every step must carry a hard per-step timeout — no exceptions, no defaults.
    """
    import scripts.clock_supervisor as sup

    assert sup.STEPS, "no steps defined"
    for step in sup.STEPS:
        assert step.timeout_s and step.timeout_s > 0, f"step {step.name!r} has no timeout"
