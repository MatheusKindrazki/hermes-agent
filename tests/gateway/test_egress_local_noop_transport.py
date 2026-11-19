"""The external egress seam gates real transports, not the no-op local route.

A canonical local session publishes its reply through the durable event stream; the
LocalSessionAdapter ``send`` reaches nothing outside the process. Before this fix, a shadow or
enforced tone gate turned every local final into ``egress_policy_rejected`` although nothing was
withheld, while external adapters must keep failing closed without a tone envelope.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.egress_policy import EgressPolicy
from gateway.external_egress import deliver_external_action
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.reliability_outbox import ReliabilityOutbox
from gateway.session_local import LocalSessionAdapter


def _owner(tmp_path, mode):
    config = {
        "gateway": {
            "reliability": {
                "egress": {
                    "mode": mode,
                    "profile": "default",
                    "tenant": "personal",
                    "machine": "mini",
                    "work_id": "01a04a0f-455a-7bea-a064-4aa68b009d39",
                    "policy_version": "tone-v1",
                },
                "outbox": {"mode": "off"},
            }
        }
    }
    return SimpleNamespace(
        _egress_policy=EgressPolicy.from_config(config),
        _reliability_outbox=ReliabilityOutbox.from_config(config, hermes_home=tmp_path),
    )


class _ExternalAdapter:
    """Stands in for any network transport (webhook, Telegram, Slack)."""


async def _deliver_through_seam(owner, adapter, calls):
    async def deliver(prepared):
        calls.append(prepared)
        return SendResult(success=True, message_id="native-1")

    return await deliver_external_action(
        owner=owner,
        adapter=adapter,
        chat_id="local-abc",
        action="send",
        content=b'{"action":"_send_with_retry"}',
        payload={"action": "_send_with_retry"},
        metadata={"thread_id": "T1"},
        event_type="gateway_adapter_final",
        milestone="turn-complete",
        deliver=deliver,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_local_route_final_is_not_rejected_without_tone_envelope(tmp_path, mode):
    adapter = LocalSessionAdapter(SimpleNamespace(runner=None))
    calls = []

    result = await _deliver_through_seam(_owner(tmp_path, mode), adapter, calls)

    assert result.success is True
    assert calls == [{"thread_id": "T1"}]  # caller metadata reaches the no-op send unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_external_transport_still_fails_closed_without_tone_envelope(tmp_path, mode):
    calls = []

    result = await _deliver_through_seam(_owner(tmp_path, mode), _ExternalAdapter(), calls)

    assert result.success is False
    assert result.error == "egress_policy_rejected"
    assert calls == []  # the native transport is never reached


def test_only_the_local_route_declares_no_external_transport():
    assert LocalSessionAdapter.carries_no_external_transport is True
    assert getattr(BasePlatformAdapter, "carries_no_external_transport", False) is False
    subclasses = [
        cls
        for cls in BasePlatformAdapter.__subclasses__()
        if getattr(cls, "carries_no_external_transport", False) is True
    ]
    assert subclasses == [LocalSessionAdapter]
