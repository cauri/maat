"""#291 — layer-boundary guard (the issue's "tiny grep step", run in CI via pytest).

Holds the one-directional rule so the agents↔serving strain can't silently return:
- agents never import a ``serving`` PRIVATE (a name starting with ``_``);
- agents never use an in-function ``maat.serving`` import (the circular-dependency dodge #291 removed).

Agents may still import a serving PUBLIC API at module scope (e.g. triage reads the public
feedback queue) — that is allowed and does not create a cycle.
"""

import re
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[1] / "maat" / "agents"


def _agent_lines():
    for f in sorted(AGENTS.rglob("*.py")):
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
