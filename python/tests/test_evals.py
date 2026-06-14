"""Eval-harness tests — the golden-regression guard must pass clean and catch over-merge."""

from maat.evals import evaluate

GOOD = [
    {
        "fact": "Valoria's finance minister, Daniel Reyes, resigned",
        "sources": ["A", "B", "C", "D", "E"],
        "independent_originators": 3,
        "has_primary": True,
        "confidence": 0.97,
        "extremity": "ordinary",
    },
    {
        "fact": "Valoria's central bank secretly sold its entire gold reserve",
        "sources": ["X", "Y", "Z"],
        "independent_originators": 1,
        "has_primary": False,
        "confidence": 0.32,
        "extremity": "extraordinary",
    },
]

EXP = {
    "resignation": {
        "match": "Reyes",
        "independent_originators": 3,
        "extremity": "ordinary",
        "has_primary": True,
        "label": "Well corroborated",
    },
    "gold": {
        "match": "gold reserve",
        "max_independent_originators": 1,
        "extremity": "extraordinary",
        "has_primary": False,
        "label": "Thinly sourced",
    },
}


def test_eval_passes_on_good_projections():
    report = evaluate(GOOD, [{"kind": "fact"}, {"kind": "projection"}], EXP)
    assert report["passed"]
    assert report["metrics"]["clusters"] == 2
    assert report["metrics"]["extraordinary"] == 1


def test_eval_catches_overmerge_regression():
    # the #20 bug shape: the two stories chained into one 8-source cluster
    merged = [
        {
            "fact": "Valoria's finance minister Reyes resigned ... central bank gold reserve",
            "sources": list("ABCDEFGH"),
            "independent_originators": 4,
            "has_primary": True,
            "confidence": 0.89,
            "extremity": "extraordinary",
        }
    ]
    report = evaluate(merged, [], EXP)
    assert not report["passed"]  # wrong originator count + extremity -> golden check fails
