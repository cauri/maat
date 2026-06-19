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


def _vec_literal(vec: list[float]) -> str:
    """A pgvector text literal — ``[1.0,2.0,...]`` — for ``$n::vector`` inserts (no codec needed)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _parse_vec(text: str) -> list[float]:
    """Parse pgvector's ``embedding::text`` form (``[1,2,3]``) back into a float list."""
    s = (text or "").strip()
    return [float(x) for x in s[1:-1].split(",")] if len(s) > 2 else []


async def embeddings_for(texts: list[str]) -> np.ndarray:
    """Embedding matrix aligned row-for-row with ``texts``.

    Reuses the cache for texts seen before and embeds only the unseen ones (chunked), persisting the
    new vectors. Returns an ``(len(texts), dim)`` float64 array. Falls back to a plain chunked embed
    if the cache table is unavailable.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float64)
    hashes = [ids.text_fingerprint(t) for t in texts]
    rep: dict[str, str] = {}  # one representative text per distinct hash (identical texts share a vec)
    for h, t in zip(hashes, texts):
        rep.setdefault(h, t)
    uniq = list(rep)

    pool = await get_pool()
    try:
        try:
            cached: dict[str, list[float]] = {
                r["text_hash"]: _parse_vec(r["emb"])
                for r in await pool.fetch(
                    "select text_hash, embedding::text emb from embedding_cache "
                    "where text_hash = any($1)",
                    uniq,
                )
            }
        except asyncpg.UndefinedTableError:  # cache not migrated yet — embed everything this run
            vecs = dict(zip(uniq, _embed_chunked([rep[h] for h in uniq])))
            return np.asarray([vecs[h] for h in hashes], dtype=np.float64)
        missing = [h for h in uniq if h not in cached]
        if missing:
            for h, vec in zip(missing, _embed_chunked([rep[h] for h in missing])):
                cached[h] = vec
            await pool.executemany(
                "insert into embedding_cache (text_hash, embedding, model) "
                "values ($1, $2::vector, $3) on conflict (text_hash) do nothing",
                [(h, _vec_literal(cached[h]), MISTRAL_EMBED) for h in missing],
            )
    finally:
        await pool.close()
    return np.asarray([cached[h] for h in hashes], dtype=np.float64)
