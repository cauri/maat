"""#291/#293 — layer-boundary guard (the issue's "tiny grep step", run in CI via pytest).

Holds the one-directional rule, both arms, so the agents↔serving/pipeline strain can't
silently return:
- agents never import a ``serving`` PRIVATE (a name starting with ``_``);
- agents never use an in-function ``maat.serving`` import (the circular-dependency dodge #291 removed);
- ``serving`` and ``pipeline`` never import ``maat.agents`` at all — they sit BELOW agents in the
  layering, so the dependency only ever points downward (#293). This is the leak #293's audit
  found: ``serving/feed.py`` and ``prompts.py`` were reaching up into ``maat.agents.curation``; it
  stays fixed because the pure curation/triage cores now live under ``maat.pipeline``.

Agents may still import a serving/pipeline PUBLIC API at module scope (e.g. triage reads the public
feedback queue, curation reads the pipeline ranking core) — that is allowed and does not create a
cycle.
"""

import re
from pathlib import Path

MAAT = Path(__file__).resolve().parents[1] / "maat"
AGENTS = MAAT / "agents"
SERVING = MAAT / "serving"
PIPELINE = MAAT / "pipeline"


def _agent_lines():
    for f in sorted(AGENTS.rglob("*.py")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            yield f, n, line


def _module_lines(root):
    for f in sorted(root.rglob("*.py")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            yield f, n, line


def test_agents_never_import_a_serving_private():
    offenders = []
    for f, n, line in _agent_lines():
        m = re.search(r"from\s+maat\.serving\.[\w.]+\s+import\s+(.+)", line)
        if not m:
            continue
        names = [x.split(" as ")[0].strip().strip("()") for x in m.group(1).split(",")]
        if any(name.startswith("_") for name in names if name):
            offenders.append(f"{f.relative_to(AGENTS.parent)}:{n}: {line.strip()}")
    assert not offenders, "agents reaching into serving privates (#291):\n" + "\n".join(offenders)


def test_agents_have_no_in_function_serving_imports():
    offenders = [
        f"{f.relative_to(AGENTS.parent)}:{n}: {line.strip()}"
        for f, n, line in _agent_lines()
        if re.match(r"[ \t]+(from|import)\s+maat\.serving\b", line)
    ]
    assert not offenders, "in-function serving imports in agents — the circular dodge (#291):\n" + "\n".join(offenders)


def test_serving_and_pipeline_never_import_agents():
    """Reverse arm of the one-directional rule (#291, audit #293): ``serving`` and ``pipeline`` sit
    BELOW ``agents`` — agents consume them, never the other way round — so they must never import
    ``maat.agents`` (module scope or in-function). Catches the serving→agents.curation leak #293
    found; passes now that the pure curation/triage cores live under ``maat.pipeline``."""
    offenders = []
    for root in (SERVING, PIPELINE):
        for f, n, line in _module_lines(root):
            if re.match(r"[ \t]*(from|import)\s+maat\.agents\b", line):
                offenders.append(f"{f.relative_to(MAAT)}:{n}: {line.strip()}")
    assert not offenders, (
        "serving/pipeline importing maat.agents — the reverse layer leak (#291/#293):\n"
        + "\n".join(offenders)
    )
