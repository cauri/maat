"""#187 — folding admin.source.flagged into the current allow/deny sets."""

from maat.serving.source_flags import denied_sources, fold_source_flags


def test_latest_flag_per_source_wins():
    events = [
        {"source": "spam.example", "status": "deny"},
        {"source": "reuters.com", "status": "allow"},
        {"source": "spam.example", "status": "allow"},  # operator changed their mind
        {"source": "gamed.example", "status": "deny"},
    ]
    assert fold_source_flags(events) == {
        "spam.example": "allow",
        "reuters.com": "allow",
        "gamed.example": "deny",
    }
    assert denied_sources(events) == {"gamed.example"}


def test_accepts_json_strings_and_ignores_junk():
    import json

    events = [json.dumps({"source": "x.example", "status": "deny"}), {"source": "y", "status": "??"}]
    assert denied_sources(events) == {"x.example"}


def test_empty():
    assert denied_sources([]) == set()
