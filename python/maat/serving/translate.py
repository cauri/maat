"""Cloud translate-for-display (#54) — the fallback the Apple client calls when its on-device
Apple Translation can't handle a language pair. Translate for DISPLAY only; never score a
translation (§4). Routes through the provider seam (mistral_complete). DRAFT prompt — review
with cauri before relying on it in prod.
"""

from __future__ import annotations

from maat.providers.seam import mistral_complete

# DRAFT — review with cauri (in-platform agent prompt fed to Mistral; see D22/D23).
_TRANSLATE_PROMPT = (
    "Translate the text below into {target}. Output ONLY the translation — no notes, no quotes, "
    "no preamble. Preserve meaning, named entities, and numbers; do not summarise or omit.{src}"
    "\n\nTEXT:\n{text}"
)


def translate_text(
    text: str, target: str = "en", source: str | None = None, *, max_tokens: int = 1200
) -> tuple[str, str]:
    """Translate ``text`` into ``target`` for display.

    Returns ``(translation, engine)`` where engine is ``"mistral"`` on success or ``"identity"``
    when the text is empty or the provider is unavailable (no key / error) — so the reader
    degrades to showing the original text rather than breaking.
    """
    if not text.strip():
        return text, "identity"
    src = f" The source language is {source}." if source else ""
    try:
        reply = mistral_complete(
            _TRANSLATE_PROMPT.format(target=target, src=src, text=text[:6000]),
            max_tokens=max_tokens,
        )
        out = (reply.text or "").strip()
        return (out, "mistral") if out else (text, "identity")
    except Exception:
        return text, "identity"
