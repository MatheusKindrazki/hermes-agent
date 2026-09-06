"""K10 cross-repo K11 contract at the universal egress seam."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gateway.egress_policy import EgressPolicy


POS = Path(os.environ["HERMES_TEST_K7_ROOT"]) if os.environ.get("HERMES_TEST_K7_ROOT") else None
requires_pos = pytest.mark.skipif(POS is None, reason="cross-repo unavailable: HERMES_TEST_K7_ROOT required (real K11 gate)")
WORK_ID = "01a04a0f-455a-7bea-a064-4aa68b009d39"


def _envelope(**changes):
    result = {
        "schema_version": "tone-envelope.v1", "mode": "enforce",
        "profile": "luguistaff", "tenant": "lugui", "channel": "hermes",
        "template": "completion",
        "facts": {"work_id": WORK_ID, "status": "completed", "numbers": "21,51",
                  "links": "https://example.test/work", "evidence": "pytest: 1 passed",
                  "risk": "normal", "decision_request": ""},
        "voice": {"register": "direct", "locale": "pt-BR"},
    }
    result.update(changes)
    return result


def _policy(monkeypatch, *, mode="enforce", sha=None):
    gate = POS / "cron/scripts/tone-gate.py"
    schema = POS / "control/schemas/tone-envelope.schema.json"
    monkeypatch.setenv("HERMES_TONE_GATE_BIN", str(gate))
    monkeypatch.setenv("HERMES_TONE_SCHEMA_SHA256", sha or hashlib.sha256(schema.read_bytes()).hexdigest())
    return EgressPolicy.from_config({"gateway": {"reliability": {"egress": {"mode": mode}}}})


def _metadata(envelope):
    return {"tone_envelope": envelope, "profile": "luguistaff", "tenant": "lugui",
            "machine": "personal-mac-mini", "work_id": WORK_ID,
            "session_id": "bot-session", "policy_version": "tone-v1"}


@requires_pos
def test_real_k11_gate_accepts_exact_schema_and_preserves_facts(monkeypatch):
    prepared, decision = _policy(monkeypatch).prepare_metadata(
        action="send", destination="hermes:bot-chat", content=b"conclusao",
        metadata=_metadata(_envelope()),
    )
    assert decision.allowed is True
    receipt = prepared["tone_gate"]["receipt"]
    assert receipt["schema_version"] == "tone-envelope.v1"
    assert receipt["facts_match"] is True


@requires_pos
def test_k11_schema_pin_mismatch_or_fact_corruption_fails_closed(monkeypatch):
    bad_sha = "0" * 64
    _, decision = _policy(monkeypatch, sha=bad_sha).prepare_metadata(
        action="send", destination="hermes:bot-chat", content=b"conclusao",
        metadata=_metadata(_envelope()),
    )
    assert decision.allowed is False
    assert "tone_gate_rejected" in decision.reasons or "tone_schema_mismatch" in decision.reasons

    broken = _envelope()
    broken["facts"] = dict(broken["facts"], status="not-a-status")
    _, decision = _policy(monkeypatch).prepare_metadata(
        action="send", destination="hermes:bot-chat", content=b"conclusao",
        metadata=_metadata(broken),
    )
    assert decision.allowed is False
