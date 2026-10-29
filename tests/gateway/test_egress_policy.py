"""Egress identity and approval policy behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent import outbound_webhooks
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.egress_policy import EgressPolicy, resolve_egress_settings
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.turn_context import RequestContext, request_context_scope


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


def test_caller_cannot_replace_request_context_expected_tenant():
    cfg = {
        "gateway": {
            "reliability": {
                "request_context": {"enabled": True},
                "egress": {
                    "mode": "enforce",
                    "machine": "personal-mac-mini",
                    "work_id": WORK_ID,
                },
            }
        }
    }
    context = RequestContext(
        profile="luguistaff",
        tenant="lugui",
        hermes_home=Path("/authority/lugui"),
        workspace=Path("/workspace/lugui"),
        model="model-a",
        provider="provider-a",
        approval="manual",
        session_id="runtime-sid",
        secret_scope_bound=True,
    )
    policy = EgressPolicy.from_config(cfg)

    with request_context_scope(context, config=cfg):
        decision = policy.evaluate_delivery(
            action="send",
            destination="slack:C123",
            content=b"lugui payload",
            metadata={
                "expected_tenant": "applause",
                "machine": "personal-mac-mini",
            },
        )

    assert decision.allowed is True
    assert decision.envelope["tenant"] == "lugui"


def test_caller_tenant_divergence_from_request_context_is_blocked():
    cfg = {
        "gateway": {
            "reliability": {
                "request_context": {"enabled": True},
                "egress": {
                    "mode": "enforce",
                    "machine": "personal-mac-mini",
                    "work_id": WORK_ID,
                },
            }
        }
    }
    context = RequestContext(
        profile="luguistaff",
        tenant="lugui",
        hermes_home=Path("/authority/lugui"),
        workspace=Path("/workspace/lugui"),
        model="model-a",
        provider="provider-a",
        approval="manual",
        session_id="runtime-sid",
        secret_scope_bound=True,
    )
    policy = EgressPolicy.from_config(cfg)

    with request_context_scope(context, config=cfg):
        decision = policy.evaluate_delivery(
            action="send",
            destination="slack:C123",
            content=b"wrong tenant",
            metadata={
                "tenant": "applause",
                "machine": "personal-mac-mini",
            },
        )

    assert decision.allowed is False
    assert "tenant_mismatch" in decision.reasons


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


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append(
            {"chat_id": chat_id, "content": content, "metadata": metadata}
        )
        return {"success": True, "message_id": "message-1"}


def _metadata(**overrides) -> dict:
    metadata = {
        "profile": "luguistaff",
        "tenant": "lugui",
        "machine": "personal-mac-mini",
        "work_id": WORK_ID,
        "session_id": "runtime-sid",
        "policy_version": "tone-v1",
    }
    metadata.update(overrides)
    return metadata


@pytest.mark.parametrize("fault", ["missing", "extra", "facts", "tenant"])
def test_unit_tone_fixture_rejects_unregistered_or_changed_envelopes(unit_tone_gate, fault):
    metadata = _metadata(tone_envelope=unit_tone_gate())
    if fault == "missing":
        del metadata["tone_envelope"]
    elif fault == "extra":
        metadata["tone_envelope"]["unexpected"] = True
    elif fault == "facts":
        metadata["tone_envelope"]["facts"]["status"] = "changed"
    else:
        metadata["tenant"] = "foreign"
    _, decision = EgressPolicy.from_config(_config("enforce")).prepare_metadata(
        action="send", destination="slack:C123", content=b"unit", metadata=metadata,
    )
    assert decision.allowed is False
    assert "unit_tone_envelope_invalid" in decision.reasons


def test_unit_tone_fixture_does_not_bypass_identity(unit_tone_gate):
    metadata = _metadata(tone_envelope=unit_tone_gate())
    del metadata["work_id"]
    prepared, decision = EgressPolicy.from_config(_config("enforce")).prepare_metadata(
        action="send", destination="slack:C123", content=b"unit", metadata=metadata,
    )
    assert prepared["tone_gate"]["allowed"] is True
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_delivery_calls_gate_once_and_blocks_before_native_adapter(unit_tone_gate):
    adapter = _RecordingAdapter()
    policy = EgressPolicy.from_config(_config("enforce"))
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.SLACK: adapter},
        egress_policy=policy,
    )
    target = DeliveryTarget(platform=Platform.SLACK, chat_id="C123")

    blocked = await router._deliver_to_platform(target, "blocked", metadata={})
    delivered = await router._deliver_to_platform(
        target, "allowed", metadata=_metadata(tone_envelope=unit_tone_gate())
    )

    assert blocked["success"] is False
    assert blocked["error"] == "egress_policy_rejected"
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["chat_id"] == "C123"
    assert adapter.calls[0]["content"] == "allowed"
    assert "egress_policy" in adapter.calls[0]["metadata"]
    assert delivered == {"success": True, "message_id": "message-1"}


class _RelayTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, str | None]] = []

    async def send_outbound(self, action, *, platform=None):
        self.calls.append((action, platform))
        return {"success": True, "message_id": "relay-1"}


def _relay(transport: _RelayTransport) -> RelayAdapter:
    return RelayAdapter(
        PlatformConfig(enabled=True),
        CapabilityDescriptor(
            contract_version=CONTRACT_VERSION,
            platform="slack",
            label="Slack",
            max_message_length=4000,
            supports_draft_streaming=False,
            supports_edit=True,
            supports_threads=True,
            markdown_dialect="slack",
            len_unit="chars",
        ),
        transport=transport,
    )


@pytest.mark.asyncio
async def test_relay_send_and_edit_cannot_bypass_enforced_gate(unit_tone_gate):
    transport = _RelayTransport()
    relay = _relay(transport)
    relay._egress_policy = EgressPolicy.from_config(_config("enforce"))

    blocked_send = await relay.send("C123", "blocked", metadata={})
    blocked_edit = await relay.edit_message("C123", "M1", "blocked", metadata={})
    allowed_send = await relay.send("C123", "allowed", metadata=_metadata(tone_envelope=unit_tone_gate()))
    allowed_edit = await relay.edit_message(
        "C123", "M1", "allowed edit", metadata=_metadata(tone_envelope=unit_tone_gate())
    )

    assert blocked_send.success is False
    assert blocked_edit.success is False
    assert [call[0]["op"] for call in transport.calls] == ["send", "edit"]
    assert allowed_send.success is True
    assert allowed_edit.success is True


@pytest.mark.asyncio
async def test_runner_stream_relay_derives_identity_before_adapter(unit_tone_gate):
    """Placement metadata is not an identity envelope.

    The runner-created consumer must carry the task-local authority into the
    Relay gate so a legitimate thread/scope send is accepted without callers
    duplicating tenant/profile/work identity in arbitrary metadata.
    """
    transport = _RelayTransport()
    relay = _relay(transport)
    cfg = {
        "gateway": {
            "reliability": {
                "request_context": {"enabled": True},
                "egress": {
                    "mode": "enforce",
                    "machine": "personal-mac-mini",
                    "work_id": WORK_ID,
                    "policy_version": "tone-v1",
                },
            }
        }
    }
    policy = EgressPolicy.from_config(cfg)
    relay._egress_policy = policy
    request_context = RequestContext(
        profile="luguistaff",
        tenant="lugui",
        hermes_home=Path("/authority/lugui"),
        workspace=Path("/workspace/lugui"),
        model="model-a",
        provider="provider-a",
        approval="approval-a",
        session_id="runtime-sid",
        secret_scope_bound=True,
    )

    with request_context_scope(request_context, config=cfg):
        consumer = GatewayStreamConsumer(
            relay,
            "C123",
            StreamConsumerConfig(
                transport="edit", edit_interval=0.01,
                buffer_threshold=1, cursor="",
            ),
            metadata={"thread_id": "T123", "scope_id": "lugui-workspace", "tone_envelope": unit_tone_gate()},
            egress_policy=policy,
        )
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("legitimate final")
        consumer.finish("legitimate final")
        await task

    assert [call[0]["op"] for call in transport.calls] == ["send"]
    assert consumer.final_response_sent is True


def test_outbound_webhook_cannot_bypass_enforced_gate(monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(outbound_webhooks, "_enqueue", queued.append)
    target = outbound_webhooks.WebhookTarget(
        url="https://example.invalid/hook",
        events=["on_session_end"],
    )
    callback = outbound_webhooks._make_callback(
        "on_session_end",
        target,
        egress_policy=EgressPolicy.from_config(_config("enforce")),
    )

    callback(session_id="runtime-sid")
    callback(**_metadata())

    assert len(queued) == 1
