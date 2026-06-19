"""Unit tests for the console command/query API (#304) — the pure pieces: the command table
(payload builders + validation) and the SSE envelope shaping. No DB or bus required."""

from __future__ import annotations

import pytest

from maat import config, events
from maat import prompts as prompts_mod
from maat.serving.console_api import COMMANDS, command_manifest, event_to_sse


def _enactable_key() -> str:
    return sorted(config._ENACTABLE)[0]


def _editable_prompt_key() -> str:
    return sorted(prompts_mod.EDITABLE_KEYS)[0]


# ── manifest ────────────────────────────────────────────────────────────────────────────


def test_manifest_covers_every_command_and_flags_signoff():
    manifest = {m["name"]: m for m in command_manifest()}
    assert manifest.keys() == COMMANDS.keys()
    # The veracity-core mutations must be sign-off-gated (D28).
    assert manifest["config.promote"]["requires_signoff"] is True
    assert manifest["prompt.update"]["requires_signoff"] is True
    # An ordinary correction is audited but not sign-off-gated.
    assert manifest["claim.correct"]["requires_signoff"] is False
    # Every entry names a real event type.
    for m in manifest.values():
        assert m["event_type"]
        assert isinstance(m["fields"], list)


def test_every_command_maps_to_a_known_admin_event_type():
    for spec in COMMANDS.values():
        assert spec.event_type in events.ADMIN_EVENT_TYPES


# ── builders: happy paths ─────────────────────────────────────────────────────────────────


def test_claim_correct_builds_payload_with_actor_and_fields():
    spec = COMMANDS["claim.correct"]
    stream_id, data = spec.build({"claim_id": "c1", "kind": "fact"}, "op@x.com", "wrong kind")
    assert stream_id == "c1"
    assert data == {"target": "c1", "actor": "op@x.com", "reason": "wrong kind", "kind": "fact"}


def test_cluster_merge_uses_new_id_or_first_member_as_stream():
    spec = COMMANDS["cluster.merge"]
    stream_id, data = spec.build({"merged": ["a", "b"]}, "op", "")
    assert stream_id == "a"  # falls back to the first member
    assert data["merged"] == ["a", "b"]
    stream_id2, data2 = spec.build({"merged": ["a", "b"], "new_id": "z"}, "op", "")
    assert stream_id2 == "z"
    assert data2["target"] == "z"


def test_clock_set_coerces_paused_to_bool():
    _, data = COMMANDS["clock.set"].build({"clock": "ingestion", "paused": 1}, "op", "")
    assert data["paused"] is True
    assert data["clock"] == "ingestion"


def test_config_set_and_promote_accept_valid_keys():
    key = _enactable_key()
    sid, data = COMMANDS["config.set"].build({"key": key, "value": "0.5"}, "op", "")
    assert sid == key and data["value"] == "0.5"
    sid2, _ = COMMANDS["config.promote"].build({"key": key, "value": "0.5"}, "op", "")
    assert sid2 == key


def test_prompt_update_accepts_seed_default_text():
    key = _editable_prompt_key()
    text = prompts_mod.seed_default(key)
    sid, data = COMMANDS["prompt.update"].build({"key": key, "text": text}, "op", "tweak")
    assert sid == key
    assert data["text"] == text


# ── builders: validation ──────────────────────────────────────────────────────────────────


def test_claim_correct_requires_at_least_one_field():
    with pytest.raises(ValueError):
        COMMANDS["claim.correct"].build({"claim_id": "c1"}, "op", "")


def test_missing_required_field_raises():
    with pytest.raises(ValueError):
        COMMANDS["claim.move"].build({"claim_id": "c1", "from_cluster": "a"}, "op", "")


def test_config_set_rejects_unknown_key():
    with pytest.raises(ValueError):
        COMMANDS["config.set"].build({"key": "not.a.knob", "value": "1"}, "op", "")


def test_config_promote_rejects_non_enactable_key():
    # A real knob that isn't wired into the pipeline can't be promoted.
    non_enactable = sorted(set(config.KNOBS_BY_KEY) - set(config._ENACTABLE))
    if non_enactable:
        with pytest.raises(ValueError):
            COMMANDS["config.promote"].build({"key": non_enactable[0], "value": "1"}, "op", "")


def test_cluster_merge_needs_two_ids():
    with pytest.raises(ValueError):
        COMMANDS["cluster.merge"].build({"merged": ["only-one"]}, "op", "")


def test_source_flag_rejects_bad_status():
    with pytest.raises(ValueError):
        COMMANDS["source.flag"].build({"source": "x.com", "status": "maybe"}, "op", "")


def test_prompt_update_rejects_non_editable_key():
    with pytest.raises(ValueError):
        COMMANDS["prompt.update"].build({"key": "definitely-not-a-prompt", "text": "x"}, "op", "")


# ── SSE shaping ───────────────────────────────────────────────────────────────────────────


def test_event_to_sse_extracts_actor_and_type():
    envelope = {
        "type": "admin.source.flagged",
        "stream_id": "bbc.com",
        "data": {"target": "bbc.com", "actor": "op@x.com", "reason": "spam", "status": "deny"},
        "tenant_id": "cauri",
    }
    frame = event_to_sse(envelope, now_ms=1234)
    assert frame == {
        "type": "admin.source.flagged",
        "stream_id": "bbc.com",
        "actor": "op@x.com",
        "ts": 1234,
        "data": envelope["data"],
    }


def test_event_to_sse_tolerates_missing_data():
    frame = event_to_sse({"type": "x", "stream_id": "s"}, now_ms=1)
    assert frame["actor"] is None
    assert frame["data"] == {}
    assert frame["ts"] == 1
