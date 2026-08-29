"""Behavior contract for the default-off Hermes reliability outbox seam."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent import outbound_webhooks
from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.reliability_outbox import ReliabilityOutbox, resolve_outbox_settings


WORK_ID = "01a04a0f-455a-7bea-a064-4aa68b009d39"


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send(self, chat_id: str, content: str, metadata=None):
        self.calls.append(
            {"chat_id": chat_id, "content": content, "metadata": metadata}
        )
        return {"success": True, "message_id": "message-1"}


def _config(tmp_path: Path, mode: str) -> dict:
    return {
        "gateway": {
            "reliability": {
                "outbox": {
                    "mode": mode,
                    "db_path": str(tmp_path / "state" / "outbox.sqlite3"),
                    "payload_dir": str(tmp_path / "state" / "payloads"),
                }
            }
        }
    }


def _rows(db_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute("SELECT * FROM outbox_events ORDER BY created_at"))


def test_outbox_config_is_default_off_and_rejects_unknown_mode(tmp_path):
    assert resolve_outbox_settings({}, hermes_home=tmp_path).mode == "off"
    assert resolve_outbox_settings(
        _config(tmp_path, "not-a-mode"), hermes_home=tmp_path
    ).mode == "off"


def test_shadow_enqueue_is_durable_owner_only_and_idempotent(tmp_path):
    client = ReliabilityOutbox.from_config(
        _config(tmp_path, "shadow"), hermes_home=tmp_path
    )
    fields = {
        "producer": "hermes-agent.gateway.delivery",
        "work_id": WORK_ID,
        "event_type": "gateway_delivery",
        "milestone": "turn-complete",
        "version": "turn-7",
        "destination": "slack:C123",
    }

    first = client.enqueue(payload=b"private payload", **fields)
    second = client.enqueue(payload=b"private payload", **fields)

    assert first.inserted is True
    assert second.inserted is False
    assert second.event_id == first.event_id
    rows = _rows(client.settings.db_path)
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == first.idempotency_key
    assert rows[0]["state"] == "pending"
    payload_path = Path(rows[0]["payload_ref"])
    assert payload_path.read_bytes() == b"private payload"
    assert payload_path.stat().st_mode & 0o777 == 0o600
    assert client.settings.payload_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_delivery_off_preserves_adapter_bytes_and_shadow_only_enqueues(tmp_path):
    adapter = _RecordingAdapter()
    target = DeliveryTarget(platform=Platform.SLACK, chat_id="C123")
    legacy = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.SLACK: adapter},
        reliability_outbox=ReliabilityOutbox.from_config(
            _config(tmp_path / "off", "off"), hermes_home=tmp_path / "off"
        ),
    )

    legacy_result = await legacy._deliver_to_platform(
        target,
        "same bytes",
        metadata={"work_id": WORK_ID, "milestone": "turn-complete", "version": "1"},
    )

    assert legacy_result == {"success": True, "message_id": "message-1"}
    assert adapter.calls == [
        {
            "chat_id": "C123",
            "content": "same bytes",
            "metadata": {
                "work_id": WORK_ID,
                "milestone": "turn-complete",
                "version": "1",
            },
        }
    ]

    shadow_client = ReliabilityOutbox.from_config(
        _config(tmp_path / "shadow", "shadow"), hermes_home=tmp_path / "shadow"
    )
    shadow = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.SLACK: adapter},
        reliability_outbox=shadow_client,
    )

    shadow_result = await shadow._deliver_to_platform(
        target,
        "queued bytes",
        metadata={"work_id": WORK_ID, "milestone": "turn-complete", "version": "2"},
    )

    assert shadow_result["success"] is True
    assert shadow_result["shadow_enqueued"] is True
    assert shadow_result["delivered"] is False
    assert len(adapter.calls) == 1
    row = _rows(shadow_client.settings.db_path)[0]
    stored = json.loads(Path(row["payload_ref"]).read_text(encoding="utf-8"))
    assert stored["content"] == "queued bytes"
    assert stored["metadata"]["work_id"] == WORK_ID
    assert stored["destination"] == "slack:C123"


def test_outbound_webhook_shadow_enqueues_without_starting_network_worker(
    tmp_path, monkeypatch
):
    cfg = _config(tmp_path, "shadow")
    cfg["hooks"] = {
        "outbound": [
            {
                "url": "https://example.invalid/hermes",
                "events": ["on_session_end"],
                "name": "receipt",
            }
        ]
    }
    monkeypatch.setattr(outbound_webhooks, "_worker", None)
    target = outbound_webhooks.iter_configured_targets(cfg)[0]
    callback = outbound_webhooks._make_callback(
        "on_session_end",
        target,
        reliability_outbox=ReliabilityOutbox.from_config(cfg, hermes_home=tmp_path),
    )

    callback(session_id="session-1", work_id=WORK_ID, version="final")

    assert outbound_webhooks._worker is None
    row = _rows(tmp_path / "state" / "outbox.sqlite3")[0]
    assert row["producer"] == "hermes-agent.agent.outbound_webhooks"
    assert row["destination"] == "https://example.invalid/hermes"
    assert row["work_id"] == WORK_ID


def test_outbound_webhook_shadow_retry_is_one_byte_stable_event(tmp_path):
    cfg = _config(tmp_path, "shadow")
    target = outbound_webhooks.WebhookTarget(
        url="https://example.invalid/hermes",
        events=["on_session_end"],
        name="receipt",
    )
    callback = outbound_webhooks._make_callback(
        "on_session_end",
        target,
        reliability_outbox=ReliabilityOutbox.from_config(cfg, hermes_home=tmp_path),
    )
    event = {
        "session_id": "session-1",
        "work_id": WORK_ID,
        "milestone": "turn-complete",
        "version": "final-v1",
    }

    callback(**event)
    first_row = _rows(tmp_path / "state" / "outbox.sqlite3")[0]
    first_payload = Path(first_row["payload_ref"]).read_bytes()
    callback(**event)

    rows = _rows(tmp_path / "state" / "outbox.sqlite3")
    assert len(rows) == 1
    assert Path(rows[0]["payload_ref"]).read_bytes() == first_payload
