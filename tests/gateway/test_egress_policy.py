"""Egress identity and approval policy behavior."""

from __future__ import annotations

import pytest

from gateway.egress_policy import EgressPolicy, resolve_egress_settings


WORK_ID = "01a04a0f-455a-7bea-a064-4aa68b009d39"
IDENTITY_FIELDS = (
    "profile",
    "tenant",
    "machine",
    "work_id",
    "session_id",
    "policy_version",
)


def _config(mode: str) -> dict:
    return {"gateway": {"reliability": {"egress": {"mode": mode}}}}


def _envelope(**overrides) -> dict:
    envelope = {
        "schema": "kindra.egress/v1",
        "profile": "luguistaff",
        "tenant": "lugui",
        "machine": "personal-mac-mini",
        "work_id": WORK_ID,
        "session_id": "runtime-sid",
        "policy_version": "tone-v1",
        "action": "send",
        "risk": "normal",
        "explicit_override": False,
        "idempotency_key": "a" * 64,
        "payload_ref": "owner-only://payload/1",
    }
    envelope.update(overrides)
    return envelope


def test_egress_mode_is_default_off_and_unknown_mode_fails_safe_to_off():
    assert resolve_egress_settings({}).mode == "off"
    assert resolve_egress_settings(_config("unknown")).mode == "off"


@pytest.mark.parametrize("missing", IDENTITY_FIELDS)
def test_enforce_fails_closed_for_each_missing_identity_field(missing):
    envelope = _envelope()
    del envelope[missing]

    decision = EgressPolicy.from_config(_config("enforce")).evaluate(
        envelope,
        expected_profile="luguistaff",
        expected_tenant="lugui",
    )

    assert decision.allowed is False
    assert f"missing_identity:{missing}" in decision.reasons


@pytest.mark.parametrize(
    ("field", "actual", "expected_reason"),
    (
        ("tenant", "applause", "tenant_mismatch"),
        ("profile", "applausestaff", "profile_mismatch"),
    ),
)
def test_enforce_rejects_identity_mismatch(field, actual, expected_reason):
    decision = EgressPolicy.from_config(_config("enforce")).evaluate(
        _envelope(**{field: actual}),
        expected_profile="luguistaff",
        expected_tenant="lugui",
    )

    assert decision.allowed is False
    assert expected_reason in decision.reasons


def test_high_risk_requires_approval_and_shadow_only_reports_would_block():
    high_risk = _envelope(risk="high")

    enforced = EgressPolicy.from_config(_config("enforce")).evaluate(high_risk)
    shadow = EgressPolicy.from_config(_config("shadow")).evaluate(high_risk)

    assert enforced.allowed is False
    assert enforced.decision == "rejected"
    assert "approval_required" in enforced.reasons
    assert shadow.allowed is True
    assert shadow.would_block is True
    assert shadow.metadata()["payload_ref"] == "owner-only://payload/1"
    assert "content" not in shadow.metadata()


def test_approved_repaired_and_explicit_override_decisions_are_distinct():
    policy = EgressPolicy.from_config(_config("enforce"))

    approved = policy.evaluate(_envelope())
    repaired = policy.evaluate(_envelope(tone_repaired=True))
    override = policy.evaluate(
        _envelope(
            risk="high",
            explicit_override=True,
            approval_ref="approval://human/42",
        )
    )

    assert (approved.allowed, approved.decision) == (True, "approved")
    assert (repaired.allowed, repaired.decision) == (True, "repaired")
    assert (override.allowed, override.decision) == (True, "explicit_override")
