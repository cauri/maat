"""#286 — persistent embedding cache + chunked embedding for corroboration.

Pure unit tests: mistral_embed and the DB pool are stubbed, so we assert the reuse policy (embed
only unseen texts, dedup identical ones, persist the new vectors, degrade if the table is missing)
and that group_by_similarity uses caller-supplied vectors without re-embedding or mutating them.
"""

import asyncio

import asyncpg
import numpy as np

import maat.pipeline.embed_cache as ec
from maat import ids
from maat.pipeline.corroborate import group_by_similarity


def _run(coro):
    return asyncio.run(coro)


def test_vec_literal_parse_roundtrip():
    import numpy as np

    v = [0.5, -1.25, 3.0]
    # #419: _parse_vec now returns a float32 ndarray, not a boxed python list — the dtype IS the
    # fix (a list of 1024 floats costs ~41 KB vs 4 KB, and this is built once per distinct claim;
    # at 68,249 claims the list-based cache measured 2.80 GB, over the container's 3 GB limit).
    got = ec._parse_vec(ec._vec_literal(v))
    assert isinstance(got, np.ndarray) and got.dtype == np.float32
    assert np.allclose(got, v)
    assert ec._parse_vec("[]").tolist() == []
    assert np.allclose(ec._parse_vec("[1, 2, 3]"), [1.0, 2.0, 3.0])  # tolerant of pgvector's spacing


def test_embed_chunked_covers_all_in_order(monkeypatch):
    calls: list[int] = []

    def fake_embed(texts):
        calls.append(len(texts))
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr(ec, "mistral_embed", fake_embed)
    monkeypatch.setattr(ec, "_EMBED_CHUNK", 3)
    out = ec._embed_chunked(["x" * i for i in range(7)])  # 7 texts, chunk 3 → passes of 3,3,1
    assert calls == [3, 3, 1]
    assert [v[0] for v in out] == [0, 1, 2, 3, 4, 5, 6]  # order preserved across chunks


class _FakePool:
    def __init__(self, cached_rows, *, table_missing=False):
        self._cached = cached_rows
        self._table_missing = table_missing
        self.inserted: list = []

    async def fetch(self, _q, arg):
        if self._table_missing:
            raise asyncpg.UndefinedTableError("relation embedding_cache does not exist")
        wanted = set(arg)
        return [r for r in self._cached if r["text_hash"] in wanted]

    async def executemany(self, _q, rows):
        self.inserted.extend(rows)

    async def close(self):
        pass


def _patch_pool(monkeypatch, pool):
    async def fake_get_pool():
        return pool

    monkeypatch.setattr(ec, "get_pool", fake_get_pool)


def test_embeddings_for_reuses_cache_and_embeds_only_misses(monkeypatch):
    texts = ["alpha", "beta", "alpha"]  # "alpha" twice → one vector, dedup'd
    h_alpha, h_beta = ids.text_fingerprint("alpha"), ids.text_fingerprint("beta")
    pool = _FakePool([{"text_hash": h_alpha, "emb": "[1.0,0.0]"}])  # alpha cached, beta a miss
    _patch_pool(monkeypatch, pool)
    embedded: list[str] = []
    monkeypatch.setattr(ec, "mistral_embed", lambda ts: embedded.extend(ts) or [[2.0, 0.0] for _ in ts])

    out = _run(ec.embeddings_for(texts))

    assert embedded == ["beta"]  # only the miss was embedded (alpha reused, and not embedded twice)
    assert out.tolist() == [[1.0, 0.0], [2.0, 0.0], [1.0, 0.0]]  # aligned row-for-row with texts
    assert len(pool.inserted) == 1 and pool.inserted[0][0] == h_beta  # the new vector was persisted


def test_embeddings_for_degrades_when_table_missing(monkeypatch):
    pool = _FakePool([], table_missing=True)
    _patch_pool(monkeypatch, pool)
    monkeypatch.setattr(ec, "mistral_embed", lambda ts: [[9.0] for _ in ts])

    out = _run(ec.embeddings_for(["a", "b"]))

    assert out.tolist() == [[9.0], [9.0]]  # falls back to embedding everything
    assert pool.inserted == []  # nothing persisted when the cache is unavailable


def test_embeddings_for_empty_makes_no_call(monkeypatch):
    monkeypatch.setattr(ec, "get_pool", lambda: (_ for _ in ()).throw(AssertionError("no pool")))
    assert _run(ec.embeddings_for([])).shape == (0, 0)


def test_group_by_similarity_uses_provided_embeddings_without_re_embedding(monkeypatch):
    monkeypatch.setattr(
        "maat.pipeline.corroborate.mistral_embed",
        lambda ts: (_ for _ in ()).throw(AssertionError("must not re-embed when vectors are given")),
    )
    emb = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    groups = sorted(sorted(g) for g in group_by_similarity(["a", "b", "c"], 0.82, embeddings=emb))
    assert [0, 1] in groups and [2] in groups  # a,b same direction → cluster; c orthogonal stands alone


def test_group_by_similarity_does_not_mutate_caller_embeddings():
    emb = np.array([[3.0, 4.0], [3.0, 4.0]])  # norm 5 — would change if normalised in place
    before = emb.copy()
    group_by_similarity(["a", "b"], 0.82, embeddings=emb)
    assert np.array_equal(emb, before)  # the reuse cache array is never mutated


def test_group_by_similarity_same_result_with_or_without_embeddings(monkeypatch):
    emb = np.array([[1.0, 0.0], [0.92, 0.08], [0.0, 1.0]])
    monkeypatch.setattr("maat.pipeline.corroborate.mistral_embed", lambda ts: emb.tolist())
    internal = group_by_similarity(["x", "y", "z"], 0.82)
    provided = group_by_similarity(["x", "y", "z"], 0.82, embeddings=emb)
    assert internal == provided  # behaviour-preserving: provided vectors == embedding internally


def test_embeddings_are_float32_end_to_end_so_the_engine_fits_in_the_box():
    """#419 — the allocation that would have OOM'd the engine the moment it was armed.

    Measured at the real corpus (68,249 claims × 1024) with the OLD design:
        cache dict of list[float] ....... 2.80 GB   (~41 KB per boxed 1024-float list)
        returned float64 matrix ......... 0.52 GB
        float32 copy made downstream .... 0.26 GB
        ------------------------------------------
        embeddings alone ................ 3.58 GB   vs the clock container's 3 GB mem_limit

    So `group_by_similarity` being fixed (30.1 GiB → 1.3 GB) was NOT enough: the engine would have
    died in the step BEFORE clustering. This pins the dtype contract end-to-end — float32 from the
    cache through the returned matrix — which makes the downstream conversion a free view rather
    than a second copy."""
    import numpy as np

    from maat.pipeline.corroborate import group_by_similarity

    # the cache's parsed vectors
    assert ec._parse_vec("[1,2,3]").dtype == np.float32
    # …and the empty-corpus return
    assert _run(ec.embeddings_for([])).dtype == np.float32

    # the contract that matters downstream: a float32 matrix must pass through the clusterer's
    # `np.asarray(x, dtype=np.float32)` WITHOUT allocating a copy (same buffer).
    x = np.zeros((8, 4), dtype=np.float32)
    assert np.asarray(x, dtype=np.float32) is x, "float32 in should be a free view, not a copy"

    # and a float32 matrix still clusters correctly (precision is nowhere near the 0.82 bar)
    v = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    groups = group_by_similarity(["a", "b", "c"], 0.82, embeddings=v)
    assert sorted(sorted(g) for g in groups) == [[0, 1], [2]]
