"""Universal last-mile policy/outbox seam for gateway external delivery."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional

from gateway.egress_policy import EgressPolicy
from gateway.platforms.base import SendResult
from gateway.reliability_outbox import ReliabilityOutbox

logger = logging.getLogger(__name__)


async def deliver_external_action(
    *,
    owner: Any,
    adapter: Any,
    chat_id: str,
    action: str,
    content: bytes,
    payload: Mapping[str, Any],
    metadata: Optional[Mapping[str, Any]],
    event_type: str,
    milestone: str,
    deliver: Callable[[Optional[dict[str, Any]]], Awaitable[Any]],
    version: Optional[str] = None,
):
    """Gate, shadow-enqueue, or execute one final native delivery.

    ``owner`` is the runtime object responsible for the lane (runner or base
    adapter). Tests and configured runtimes may inject policy/outbox instances;
    otherwise both resolve from the same default-off ``config.yaml`` contract.
    """
    policy = getattr(owner, "_egress_policy", None) or EgressPolicy.from_config()
    outbox = getattr(owner, "_reliability_outbox", None) or ReliabilityOutbox.from_config()
    destination = f"{type(adapter).__name__}:{chat_id}"
    prepared, decision = policy.prepare_metadata(
        action=action,
        destination=destination,
        content=content,
        metadata=metadata,
    )
    if not decision.allowed:
        logger.warning(
            "Egress policy rejected %s to %s: %s",
            event_type,
            destination,
            ",".join(decision.reasons),
        )
        return SendResult(
            success=False,
            error="egress_policy_rejected",
            error_kind="egress_policy_rejected",
        )
    if outbox.mode == "shadow":
        digest = hashlib.sha256(content).hexdigest()
        receipt = outbox.enqueue_json(
            {
                "schema": "kindra.outbox-payload/v1",
                "delivery": dict(payload),
                "destination": destination,
                "metadata": prepared,
            },
            producer="hermes-agent.gateway",
            work_id=str(prepared.get("work_id") or ""),
            event_type=event_type,
            milestone=str(prepared.get("milestone") or milestone),
            version=str(version or digest),
            destination=destination,
        )
        return SendResult(success=True, message_id=receipt.event_id)
    if policy.mode != "off":
        prepared["_egress_policy_checked"] = decision.mode
    return await deliver(prepared or None)
