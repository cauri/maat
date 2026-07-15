"""The tick supervisor — runs the periodic pipeline steps, bounded and observable (#417).

Replaces the 13-step serial `sh -c 'while true; do STEP || echo "[clock] X error"; done'` loop that
ran the pipeline until 2026-07-14. That loop had three defects, each of which took prod down:

  * **No per-step timeout.** A hang in ANY step deadlocked every step after it AND every subsequent
    tick — forever. `contradiction` wedged for 6h+ on an O(n^2) scan, so `harvest` stopped
    snapshotting and `corroborate` was never even retried. `|| echo` catches exits, not hangs.
  * **Failures laundered to exit 0.** `|| echo "[clock] X error"` turned every crash into a log
    line nobody tails. corroborate OOM'd every tick for 27 days in silence.
  * **The container could never exit.** PID 1 was `while true`, so `restart: unless-stopped` was
    dead code and docker's healthcheck watched a heartbeat on a body doing no work.

This supervisor fixes the class, not the instance:

  * every step runs as a subprocess under a HARD wall-clock timeout (SIGTERM, then SIGKILL) — a
    hung step is killed and the tick continues;
  * every step's outcome (ok / failed / timeout / skipped) + duration is logged AND written to a
    status file, so liveness is a fact on disk rather than an inference from silence;
  * a step is only run when its gate env var is on, so a known-bad step is OFF by default rather
    than burning CPU for zero rows;
  * the loop lives in Python: an unhandled supervisor error EXITS non-zero, so the container dies
    and `restart: unless-stopped` is real again;
  * a heartbeat file is touched every tick for the docker healthcheck to read.

Deliberately still SERIAL. Concurrency is a follow-up; the bug was unbounded steps, not ordering.
Ordering matters though: cheap/durable steps (harvest — the truth-over-time snapshot) run BEFORE
the expensive experimental ones, so a failure there can't starve them.

Run: uv run --no-dev python scripts/clock_supervisor.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = Path(os.environ.get("MAAT_CLOCK_STATUS", "/tmp/clock_status.json"))
HEARTBEAT_PATH = Path(os.environ.get("MAAT_CLOCK_HEARTBEAT", "/tmp/clock_heartbeat"))


def _env_on(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default) not in ("0", "false", "no", "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Step:
    """One pipeline step. ``timeout_s`` is a HARD wall-clock bound — the defect that took prod down
    was that no step had one. ``gate`` is an env var that must be on for the step to run; ``None``
    means always run. ``paused_by`` skips the step when that env var is ON (the intake pause)."""

    name: str
    argv: list[str]
    timeout_s: int
    gate: str | None = None
    gate_default: str = "1"
    paused_by: str | None = None
    note: str = ""


def _py(*args: str) -> list[str]:
    return ["uv", "run", "--no-dev", "python", *args]


# Order matters: acquire → resolve → drain → CORROBORATE → registry → graph → HARVEST → the
# expensive/experimental refinements → feedback. harvest sits ABOVE geotag/grounding/contradiction
# deliberately: on 2026-07-14 a contradiction hang starved harvest and truth-over-time snapshots
# stopped for 25h. Cheap and durable goes first.
STEPS: list[Step] = [
    Step("acquire", _py("scripts/clock.py"), 900, paused_by="MAAT_INTAKE_PAUSED",
         note="GDELT topics"),
    Step("acquire-rss", _py("scripts/acquire_rss.py"), 900, paused_by="MAAT_INTAKE_PAUSED",
         note="balanced multipolar outlet feeds (#238)"),
    Step("acquire-locales", _py("scripts/acquire_locales.py"), 900, paused_by="MAAT_INTAKE_PAUSED",
         note="per-language/region GDELT floor (#239)"),
    Step("acquire-newsdata", _py("scripts/acquire_newsdata.py"), 900, paused_by="MAAT_INTAKE_PAUSED",
         note="paid multipolar channel"),
    Step("ownership", _py("-m", "maat.agents.ownership_agent"), 900,
         gate="MAAT_OWNERSHIP_LOOKUP", note="Wikidata controlling owners (#254)"),
    Step("translate-titles", _py("-m", "maat.agents.translate_titles"), 900,
         gate="MAAT_TRANSLATE_TITLES", note="English gloss for non-English titles (#54)"),
    Step("corroborate", _py("-m", "maat.agents.corroborate_agent"), 2400,
         gate="MAAT_CORROBORATE_ENABLED", gate_default="0",
         note="recompute clusters — THE ENGINE; gated OFF pending cauri's sign-off (#417)"),
    Step("source-registry", _py("scripts/source_registry_agent.py"), 1800,
         note="lifecycle + reputation (#241)"),
    Step("story-graph", _py("-m", "maat.agents.story_graph_agent"), 1200,
         note="thread clusters into event-nodes (#42)"),
    Step("harvest", _py("scripts/harvest.py"), 900,
         note="snapshot clusters for truth-over-time (#39) — cheap + durable, runs before the rest"),
    Step("geotag", _py("-m", "maat.agents.geotag_agent"), 900,
         gate="MAAT_CURATION_LLM", note="infer country for unplaced clusters (#189)"),
    Step("grounding", _py("-m", "maat.agents.grounding_agent"), 1200,
         gate="MAAT_GROUNDING_LLM", note="judge primary-bearing clusters vs their primary (#228)"),
    Step("contradiction", _py("-m", "maat.agents.contradiction_agent"), 900,
         gate="MAAT_CONTRADICTION_ENABLED", gate_default="0",
         note="NLI vs nearest neighbours (#229) — gated OFF: O(n^2) scan wedged the tick (#417)"),
    Step("triage", _py("-m", "maat.agents.triage"), 600,
         note="route submitted feedback (#58)"),
    Step("file-issues", _py("-m", "maat.serving.issue_filing"), 600,
         note="auto-fix feedback → tracked issues (#214)"),
]


@dataclass
class StepResult:
    name: str
    outcome: str  # ok | failed | timeout | skipped
    seconds: float = 0.0
    exit_code: int | None = None
    detail: str = ""


@dataclass
class TickResult:
    started: str = field(default_factory=_now)
    finished: str = ""
    steps: list[StepResult] = field(default_factory=list)

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if s.outcome in ("failed", "timeout")]


def _skip_reason(step: Step) -> str | None:
    """Why this step won't run this tick — or None if it should."""
    if step.paused_by and _env_on(step.paused_by, "0"):
        return f"{step.paused_by}=1"
    if step.gate and not _env_on(step.gate, step.gate_default):
        return f"{step.gate} off"
    return None


def run_step(step: Step) -> StepResult:
    """Run one step under a hard timeout. NEVER raises — a step's failure is data, not an exception:
    the whole point is that one bad step cannot take the tick (or the next 27 days) with it."""
    reason = _skip_reason(step)
    if reason:
        print(f"[clock] {step.name}: SKIPPED ({reason})", flush=True)
        return StepResult(step.name, "skipped", detail=reason)

    print(f"[clock] {step.name}: start ({step.note})", flush=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            step.argv, cwd=ROOT, timeout=step.timeout_s,
            capture_output=True, text=True, check=False,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run already SIGKILLed the child on timeout. THIS is the fix for the class of
        # failure that deadlocked prod: a hung step is bounded and the tick moves on.
        secs = round(time.monotonic() - t0, 1)
        print(f"[clock] {step.name}: TIMEOUT after {secs}s (limit {step.timeout_s}s)", flush=True)
        return StepResult(step.name, "timeout", secs, None, f"exceeded {step.timeout_s}s")
    except Exception as e:  # noqa: BLE001 - a supervisor must never die of a child's error
        secs = round(time.monotonic() - t0, 1)
        print(f"[clock] {step.name}: ERROR {type(e).__name__}: {e}", flush=True)
        return StepResult(step.name, "failed", secs, None, f"{type(e).__name__}: {e}")

    secs = round(time.monotonic() - t0, 1)
    if proc.returncode == 0:
        print(f"[clock] {step.name}: ok ({secs}s)", flush=True)
        return StepResult(step.name, "ok", secs, 0)
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = " | ".join(tail[-3:])[:500]
    print(f"[clock] {step.name}: FAILED exit={proc.returncode} ({secs}s) :: {detail}", flush=True)
    return StepResult(step.name, "failed", secs, proc.returncode, detail)


def write_status(tick: TickResult) -> None:
    """Persist the tick's per-step outcome. Liveness becomes a FACT ON DISK rather than something
    inferred from the absence of a complaint — the docker healthcheck and the watchdog read this,
    and it is what `[clock] tick done` alone could never tell you: WHICH steps actually ran."""
    payload = {
        "started": tick.started,
        "finished": tick.finished,
        "steps": [
            {"name": s.name, "outcome": s.outcome, "seconds": s.seconds,
             "exit_code": s.exit_code, "detail": s.detail}
            for s in tick.steps
        ],
        "failed": [s.name for s in tick.failed],
        "ok": sum(1 for s in tick.steps if s.outcome == "ok"),
    }
    try:
        STATUS_PATH.write_text(json.dumps(payload, indent=1))
        HEARTBEAT_PATH.write_text(tick.finished or _now())
    except OSError as e:
        print(f"[clock] WARN could not write status: {e}", flush=True)


def run_tick(steps: list[Step] | None = None) -> TickResult:
    """One full pass. Always completes — every step is bounded and every failure is recorded."""
    tick = TickResult()
    for step in steps or STEPS:
        tick.steps.append(run_step(step))
    tick.finished = _now()
    write_status(tick)
    ran = sum(1 for s in tick.steps if s.outcome == "ok")
    failed = [s.name for s in tick.failed]
    skipped = [s.name for s in tick.steps if s.outcome == "skipped"]
    print(
        f"[clock] tick done: {ran} ok, {len(failed)} failed{' (' + ', '.join(failed) + ')' if failed else ''}"
        f", {len(skipped)} skipped{' (' + ', '.join(skipped) + ')' if skipped else ''}",
        flush=True,
    )
    return tick


def main() -> int:
    interval = int(os.environ.get("MAAT_TICK_INTERVAL", "10800"))
    print(f"[clock] supervisor up — {len(STEPS)} steps, interval {interval}s", flush=True)
    while True:
        try:
            run_tick()
        except Exception as e:  # noqa: BLE001 - never spin silently; die loudly so restart: fires
            print(f"[clock] FATAL supervisor error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return 1
        print(f"[clock] sleeping {interval}s", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
