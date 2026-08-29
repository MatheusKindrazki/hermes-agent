"""Concurrent egress identities remain task-local and tenant-safe."""

from __future__ import annotations

import asyncio

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.egress_policy import EgressPolicy
from gateway.session_context import clear_session_vars, set_session_vars


WORK_ID = "01a04a0f-455a-7bea-a064-4aa68b009d39"


class _InterleavingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._both_arrived = asyncio.Event()

    async def send(self, chat_id, content, metadata=None):
        snapshot = {
            "chat_id": chat_id,
            "content": content,
            "metadata": dict(metadata or {}),
        }
        self.calls.append(snapshot)
        if len(self.calls) == 2:
            self._both_arrived.set()
        await asyncio.wait_for(self._both_arrived.wait(), timeout=1)
        return {"success": True, "message_id": f"message-{chat_id}"}


@pytest.mark.asyncio
async def test_two_concurrent_tenants_keep_distinct_egress_envelopes():
    adapter = _InterleavingAdapter()
    policy = EgressPolicy.from_config(
        {"gateway": {"reliability": {"egress": {"mode": "enforce"}}}}
    )
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.SLACK: adapter},
        egress_policy=policy,
    )

    async def deliver(profile: str, tenant: str, session_id: str, chat_id: str):
        tokens = set_session_vars(profile=profile, session_id=session_id)
        try:
            return await router._deliver_to_platform(
                DeliveryTarget(platform=Platform.SLACK, chat_id=chat_id),
                f"message for {tenant}",
                metadata={
                    "tenant": tenant,
                    "machine": "personal-mac-mini",
                    "work_id": WORK_ID,
                    "policy_version": "tone-v1",
                },
            )
        finally:
            clear_session_vars(tokens)

    results = await asyncio.gather(
        deliver("luguistaff", "lugui", "lugui-session", "C-LUGUI"),
        deliver("applausestaff", "applause", "applause-session", "C-APPLAUSE"),
    )

    assert all(result["success"] is True for result in results)
    by_chat = {call["chat_id"]: call for call in adapter.calls}
    lugui = by_chat["C-LUGUI"]["metadata"]["egress_policy"]
    applause = by_chat["C-APPLAUSE"]["metadata"]["egress_policy"]
    assert (lugui["profile"], lugui["tenant"], lugui["session_id"]) == (
        "luguistaff",
        "lugui",
        "lugui-session",
    )
    assert (applause["profile"], applause["tenant"], applause["session_id"]) == (
        "applausestaff",
        "applause",
        "applause-session",
    )
    assert lugui["payload_ref"] != applause["payload_ref"]
