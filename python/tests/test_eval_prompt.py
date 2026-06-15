"""Eval-on-change tests — orchestration with a stubbed pipeline (no live LLM), plus the pure
summary and corpus loader."""

from maat.evals import evaluate


def test_load_corpus_reads_fixtures():
    from maat.eval_prompt import load_corpus

    arts = load_corpus()
    assert arts, "expected golden corpus fixtures under corpus/*.json"
    assert all("id" in a and "body" in a for a in arts)


def test_run_goldens_threads_each_prompt_and_shapes_clusters(monkeypatch):
    import maat.eval_prompt as ep
    from maat.pipeline.claim import Claim
    from maat.pipeline.corroborate import Corroboration

    seen: dict = {}

    def fake_extract(body, *, source_metadata="", language="en", prompt=""):
        seen["extract"] = prompt
        return [Claim(text="Reyes resigned", voice="own", evidence_span="x")]

    def fake_classify(claims, *, article_text="", prompt=""):
        seen["classify"] = prompt
        return [c.model_copy(update={"kind": "fact"}) for c in claims]

    def fake_rate(fact, *, model="", prompt=""):
        seen["extremity"] = prompt
        return "significant"

    def fake_corr(rows, bodies, *, extremity_of=None, **kw):
        seen["level"] = extremity_of(rows[0].text) if extremity_of else None  # exercises the prompt
        return [
            Corroboration(
                fact="Reyes resigned", claim_ids=[r.id for r in rows], sources=["S1"],
                originators=[["a1"]], independent_originators=1, has_primary=True,
                extremity="significant", confidence=0.9,
            )
        ]

    monkeypatch.setattr(ep, "extract_claims", fake_extract)
    monkeypatch.setattr(ep, "classify_claims", fake_classify)
    monkeypatch.setattr(ep, "rate_extremity", fake_rate)
    monkeypatch.setattr(ep, "corroborate", fake_corr)

    clusters, claims = ep.run_goldens(
        [{"id": "a1", "source": "S1", "body": "Reyes resigned.", "language": "en"}],
        extract_prompt="EX", classify_prompt="CL", extremity_prompt="EXT",
    )
    assert seen == {"extract": "EX", "classify": "CL", "extremity": "EXT", "level": "significant"}
    assert clusters[0]["fact"] == "Reyes resigned" and clusters[0]["has_primary"] is True
    assert claims == [{"kind": "fact"}]


def test_summary_pass_and_fail():
    from maat.eval_prompt import summary

    clusters = [{
        "fact": "Reyes resigned", "sources": ["a", "b", "c"],
        "originators": [["a"], ["b"], ["c"]], "independent_originators": 3,
        "has_primary": True, "confidence": 0.9, "extremity": "significant",
    }]
    claims = [{"kind": "fact"}]
    ok = summary(evaluate(clusters, claims, {"resign": {"match": "Reyes", "independent_originators": 3}}))
    assert ok.startswith("PASS — 1/1")
    bad = summary(evaluate(clusters, claims, {"resign": {"match": "Reyes", "independent_originators": 9}}))
    assert bad.startswith("FAIL — 0/1") and "independent_originators" in bad
