"""NLI cross-encoder (#229) — entailment / neutral / contradiction between two claim texts.

cauri's call: an NLI MODEL, not an LLM judge — a small ONNX cross-encoder gives a calibrated
per-pair probability, cheap on CPU, and can later run on-device (#53). Gated by
``MAAT_CONTRADICTION_NLI=1``; the model is lazy-loaded and cached, and the seam is INERT (returns
None) when the flag is off — so tests and CI never touch the model or the network. Any problem
(missing dependency, download failure, runtime error) also returns None, so the contradiction agent
treats the pair as "no signal" rather than guessing.

Pinned model: ``Xenova/distilbert-base-uncased-mnli`` — a small DistilBERT fine-tuned on MNLI,
exported to ONNX (smoke-tested: correct on canonical NLI; inputs input_ids + attention_mask;
id2label order entailment / neutral / contradiction). Overridable via MAAT_NLI_MODEL /
MAAT_NLI_ONNX_FILE / MAAT_NLI_LABELS for a stronger model (e.g. a DeBERTa-v3 NLI) without code change.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache

_MODEL_REPO = os.environ.get("MAAT_NLI_MODEL") or "Xenova/distilbert-base-uncased-mnli"
_ONNX_FILE = os.environ.get("MAAT_NLI_ONNX_FILE") or "onnx/model.onnx"
# id2label order of the pinned model (index → label); set MAAT_NLI_LABELS to match a different model.
_LABELS = tuple(
    s.strip() for s in (os.environ.get("MAAT_NLI_LABELS") or "entailment,neutral,contradiction").split(",")
)


def _enabled() -> bool:
    return os.environ.get("MAAT_CONTRADICTION_NLI") == "1"


def _softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


@lru_cache(maxsize=1)
def _runtime():
    """Lazy-load (session, tokenizer); None unless enabled and the model loads cleanly.

    Gated on MAAT_CONTRADICTION_NLI so the seam never downloads a model in tests / CI.
    """
    if not _enabled() or not _MODEL_REPO:
        return None
    try:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        sess = ort.InferenceSession(
            hf_hub_download(_MODEL_REPO, _ONNX_FILE), providers=["CPUExecutionProvider"]
        )
        tok = Tokenizer.from_file(hf_hub_download(_MODEL_REPO, "tokenizer.json"))
        return sess, tok
    except Exception:
        return None


def available() -> bool:
    """Whether the NLI model is loaded — lets the agent skip the pass cleanly when it isn't."""
    return _runtime() is not None


def classify_pair(premise: str, hypothesis: str) -> tuple[str, float] | None:
    """NLI label + probability for premise→hypothesis, or None when unavailable / on error.

    label ∈ the configured labels (entailment / neutral / contradiction); score is that label's
    softmax probability.
    """
    rt = _runtime()
    if rt is None or not premise.strip() or not hypothesis.strip():
        return None
    try:
        sess, tok = rt
        enc = tok.encode(premise, hypothesis)
        feeds: dict = {"input_ids": [enc.ids], "attention_mask": [enc.attention_mask]}
        # token_type_ids only when the model expects them (BERT-family); DistilBERT/RoBERTa omit them.
        if any(i.name == "token_type_ids" for i in sess.get_inputs()):
            feeds["token_type_ids"] = [enc.type_ids]
        logits = [float(x) for x in sess.run(None, feeds)[0][0]]
        probs = _softmax(logits)
        i = max(range(len(probs)), key=lambda k: probs[k])
        label = _LABELS[i] if i < len(_LABELS) else str(i)
        return label, float(probs[i])
    except Exception:
        return None
