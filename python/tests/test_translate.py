"""#54 — cloud translate-for-display via the provider seam, with an identity fallback."""

import maat.serving.translate as tr


class _Reply:
    def __init__(self, text):
        self.text = text


def test_empty_text_is_identity():
    assert tr.translate_text("", "en") == ("", "identity")


def test_translates_via_seam(monkeypatch):
    captured = {}

    def fake(prompt, *, max_tokens=256):
        captured["prompt"] = prompt
        return _Reply("Hello world")

    monkeypatch.setattr(tr, "mistral_complete", fake)
    out, engine = tr.translate_text("Bonjour le monde", "en", "fr")
    assert out == "Hello world"
    assert engine == "mistral"
    assert "en" in captured["prompt"] and "Bonjour le monde" in captured["prompt"]


def test_falls_back_to_identity_on_provider_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no MISTRAL_API_KEY")

    monkeypatch.setattr(tr, "mistral_complete", boom)
    out, engine = tr.translate_text("Bonjour", "en")
    assert out == "Bonjour"
    assert engine == "identity"


def test_blank_model_output_falls_back(monkeypatch):
    monkeypatch.setattr(tr, "mistral_complete", lambda *a, **k: _Reply("   "))
    out, engine = tr.translate_text("Hola", "en")
    assert (out, engine) == ("Hola", "identity")
