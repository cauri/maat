"""Persistent embedding cache (#286) — reuse claim-text embeddings across corroborate runs.

Corroboration re-embedded the WHOLE claim set every tick (≈0.8 GB of Mistral calls at 100k claims,
recomputed each run). This caches each embedded text's vector, content-addressed by sha256 of the
text, in the ``embedding_cache`` pgvector table (migration 0017): an unchanged claim is embedded
once, ever, and a new cross-lingual pivot (#240) gets its own entry.

It is a DERIVED, rebuildable cache (re-embed to reconstruct) and deliberately lives OUTSIDE the
event log — a 1024-d vector per claim would bloat the append-only source of truth (#287). Embedding
the misses is chunked so the peak resident vector list stays bounded by one chunk, not the corpus.
If the cache table is not present yet (kernel migration not applied), it degrades to a plain chunked
embed — the cache is an optimisation, never a correctness dependency.
"""

from __future__ import annotations

import asyncpg
import numpy as np

from maat import ids
from maat.db import get_pool
from maat.providers.seam import MISTRAL_EMBED, mistral_embed

# Texts per mistral_embed pass — bounds the peak resident embedding LIST to one chunk (#286).
_EMBED_CHUNK = 10_000


def _embed_chunked(texts: list[str]) -> list[list[float]]:
    """``mistral_embed`` over ``texts`` in chunks (peak resident list = one chunk, not the corpus)."""
    out: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_CHUNK):
        out.extend(mistral_embed(texts[start : start + _EMBED_CHUNK]))
    return out


def _vec_literal(vec) -> str:
    """A pgvector text literal — ``[1.0,2.0,...]`` — for ``$n::vector`` inserts (no codec needed)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _parse_vec(text: str) -> np.ndarray:
    """Parse pgvector's ``embedding::text`` form (``[1,2,3]``) into a float32 vector.

    **float32 ndarray, NOT list[float] (#419).** A python list of 1024 floats costs ~41 KB (24 B per
    boxed float + list overhead) against 4 KB for the array — and this is built once per distinct
    claim. At the live corpus (68,249 claims) the cache dict alone measured **2.80 GB**, which with
    the float64 return (0.52 GB) and its float32 copy downstream (0.26 GB) put embeddings at
    **3.58 GB — over the clock container's 3 GB limit, so the engine would OOM before clustering
    anything.** Same class as the 30.1 GiB matmul: an allocation sized for a corpus of long ago.
    """
    s = (text or "").strip()
    if len(s) <= 2:
        return np.zeros((0,), dtype=np.float32)
    return np.fromstring(s[1:-1], dtype=np.float32, sep=",")


async def embeddings_for(texts: list[str]) -> np.ndarray:
    """Embedding matrix aligned row-for-row with ``texts``.

    Reuses the cache for texts seen before and embeds only the unseen ones (chunked), persisting the
    new vectors. Returns an ``(len(texts), dim)`` **float32** array. Falls back to a plain chunked
    embed if the cache table is unavailable.

    **float32, not float64 (#419).** Cosine at the same-fact bar needs nowhere near float64's precision,
    and the corpus is now large enough that the dtype is a liveness question, not a taste one: the
    float64 matrix is 0.52 GB at 68,249 claims and every downstream consumer immediately makes a
    float32 copy of it (another 0.26 GB) while the original stays alive in the caller. Returning
    float32 makes that conversion a free view and halves the resident matrix. Together with the
    float32 cache (see ``_parse_vec``) this took the embeddings path from a measured **3.58 GB —
    over the container's 3 GB limit — to ~0.5 GB.**
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    hashes = [ids.text_fingerprint(t) for t in texts]
    rep: dict[str, str] = {}  # one representative text per distinct hash (identical texts share a vec)
    for h, t in zip(hashes, texts):
        rep.setdefault(h, t)
    uniq = list(rep)

    pool = await get_pool()
    try:
        try:
            # float32 vectors, one per DISTINCT text (#419): 4 KB each, not the ~41 KB a boxed
            # python list of 1024 floats costs. This dict is the single largest live object here.
            cached: dict[str, np.ndarray] = {
                r["text_hash"]: _parse_vec(r["emb"])
                for r in await pool.fetch(
                    "select text_hash, embedding::text emb from embedding_cache "
                    "where text_hash = any($1)",
                    uniq,
                )
            }
        except asyncpg.UndefinedTableError:  # cache not migrated yet — embed everything this run
            vecs = {
                h: np.asarray(v, dtype=np.float32)
                for h, v in zip(uniq, _embed_chunked([rep[h] for h in uniq]))
            }
            return np.asarray([vecs[h] for h in hashes], dtype=np.float32)
        missing = [h for h in uniq if h not in cached]
        if missing:
            for h, vec in zip(missing, _embed_chunked([rep[h] for h in missing])):
                cached[h] = np.asarray(vec, dtype=np.float32)  # drop the boxed list immediately
            await pool.executemany(
                "insert into embedding_cache (text_hash, embedding, model) "
                "values ($1, $2::vector, $3) on conflict (text_hash) do nothing",
                [(h, _vec_literal(cached[h]), MISTRAL_EMBED) for h in missing],
            )
    finally:
        await pool.close()
    # The list here holds n REFERENCES to the cached vectors (cheap); numpy stacks them into one
    # (n, dim) float32 matrix. Peak = the cache dict + the result, both float32.
    return np.asarray([cached[h] for h in hashes], dtype=np.float32)
