"""NLI cross-encoder (#229) — entailment / neutral / contradiction between two claim texts.

cauri's call: an NLI MODEL, not an LLM judge — a small ONNX cross-encoder gives a calibrated
per-pair probability, cheap on CPU, and can later run on-device (#53). Gated by
``MAAT_CONTRADICTION_NLI=1``; the model is lazy-loaded and cached. ANY problem (no model configured,
a missing dependency, a runtime error) returns ``None`` so the contradiction agent treats the pair
as "no signal" rather than guessing — the deterministic-safe fallback.

Model repo + label order are config (``MAAT_NLI_MODEL`` / ``MAAT_NLI_LABELS``) because they are
model-specific; they're verified against the chosen model on first enable (a smoke test), never
assumed. The agent and its tests depend ONLY on ``classify_pair``, which is trivially mockable.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache

# A small MNLI cross-encoder in ONNX form, pinned + verified before MAAT_CONTRADICTION_NLI is turned
# on in prod. Empty default → the seam is inert (classify_pair returns None) until configured. Label
# order is model-specific — set MAAT_NLI_LABELS to match the chosen model's classifier head.
_MODEL_REPO = os.environ.get("MAAT_NLI_MODEL", "")
_LABELS = tuple(
    s.strip() for s in (os.environ.get("MAAT_NLI_LABELS") or "contradiction,entailment,neutral").split(",")
)


def _softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


@lru_cache(maxsize=1)
def _runtime():
    """Lazy-load (session, tokenizer); None when unavailable (no model / missing deps / no network)."""
    if not _MODEL_REPO:
        return None
    try:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        model_path = hf_hub_download(_MODEL_REPO, "model.onnx")
        tok = Tokenizer.from_pretrained(_MODEL_REPO)
        sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        return sess, tok
    except Exception:
        return None


def available() -> bool:
    """Whether the NLI model is loadable — lets the agent skip the pass cleanly when it isn't."""
    return _runtime() is not None


def classify_pair(premise: str, hypothesis: str) -> tuple[str, float] | None:
    """NLI label + probability for premise→hypothesis, or None when unavailable / on error.

    label ∈ the configured labels (contradiction / entailment / neutral); score is that label's
    softmax probability. Deterministic given the model; the only side effect is the cached load.
    """
    rt = _runtime()
    if rt is None or not premise.strip() or not hypothesis.strip():
        return None
    try:
        sess, tok = rt
        enc = tok.encode(premise, hypothesis)
        feeds: dict = {"input_ids": [enc.ids], "attention_mask": [enc.attention_mask]}
        # token_type_ids only when the model expects them (BERT-family); RoBERTa-family omits them.
        if any(i.name == "token_type_ids" for i in sess.get_inputs()):
            feeds["token_type_ids"] = [enc.type_ids]
        logits = sess.run(None, feeds)[0][0]
        probs = _softmax([float(x) for x in logits])
        i = max(range(len(probs)), key=lambda k: probs[k])
        label = _LABELS[i] if i < len(_LABELS) else str(i)
        return label, float(probs[i])
    except Exception:
        return None
