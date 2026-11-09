"""GatewayNotificationEgressMixin owns the fork delivery boundary."""
from __future__ import annotations
import json
from typing import Any, Optional, Callable, Awaitable


class GatewayNotificationEgressMixin:
    async def _deliver_external_action(
        self,
        *,
        adapter,
        chat_id: str,
        action: str,
        content: bytes,
        payload: dict[str, Any],
        metadata: Optional[dict[str, Any]],
        event_type: str,
        milestone: str,
        deliver: Callable[[Optional[dict[str, Any]]], Awaitable[Any]],
        version: Optional[str] = None,
    ):
        """Apply policy/outbox at the last seam before any native egress."""
        from gateway.external_egress import deliver_external_action

        return await deliver_external_action(
            owner=self,
            adapter=adapter,
            chat_id=chat_id,
            action=action,
            content=content,
            payload=payload,
            metadata=metadata,
            event_type=event_type,
            milestone=milestone,
            deliver=deliver,
            version=version,
        )


    async def _deliver_native_final(self, adapter, method_name: str, *, event_type: str,
                                    action: str = "send_media", **kwargs):
        metadata = kwargs.pop("metadata", None)
        payload = {"action": method_name, **kwargs}

        async def deliver(prepared_metadata):
            return await getattr(adapter, method_name)(**kwargs, metadata=prepared_metadata)

        return await self._deliver_external_action(
            adapter=adapter, chat_id=kwargs["chat_id"], action=action,
            content=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            payload=payload, metadata=metadata, event_type=event_type,
            milestone="turn-complete" if action == "send" else "media-delivery",
            deliver=deliver,
        )


    async def _deliver_external_final(
        self,
        *,
        adapter,
        chat_id: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        message_id: Optional[str] = None,
    ):
        """Gate the completed external text before send/edit reconciliation."""
        async def _deliver(prepared_metadata):
            if message_id:
                return await adapter.edit_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    content=content,
                    finalize=True,
                    metadata=prepared_metadata,
                )
            return await adapter.send(chat_id, content, metadata=prepared_metadata)

        identity = dict(metadata or {})
        return await self._deliver_external_action(
            adapter=adapter,
            chat_id=chat_id,
            action="edit" if message_id else "send",
            content=content.encode("utf-8"),
            payload={"action": "edit" if message_id else "send", "content": content},
            metadata=identity,
            event_type="gateway_runner_final",
            milestone="turn-complete",
            version=str(
                identity.get("version")
                or identity.get("client_turn_id")
                or identity.get("session_id")
                or ""
            )
            or None,
            deliver=_deliver,
        )

