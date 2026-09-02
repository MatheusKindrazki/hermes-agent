"""Delivery acknowledgement must be identity-bearing, never a bare PID."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import hashlib

from tools import bot_mode_dm


def test_delivery_receipt_binds_origin_session_and_reaches_terminal_state(tmp_path):
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("status", encoding="utf-8")
    accepted = bot_mode_dm._write_delivery_receipt(
        str(dm_file), origin_session_id="origin-exact", label="@researcher",
        origin_reason="explicit", idempotency_key=hashlib.sha256(str(tmp_path).encode()).hexdigest(),
    )
    assert accepted["state"] == "accepted"
    assert accepted["origin_session_id"] == "origin-exact"
    assert accepted["origin_reason"] == "explicit"
    assert accepted["delivery_id"]

    bot_mode_dm._update_delivery_receipt(str(dm_file), "delivered")
    persisted = json.loads((tmp_path / "message.txt.receipt.json").read_text())
    assert persisted["delivery_id"] == accepted["delivery_id"]
    assert persisted["state"] == "delivered"


def test_same_idempotency_key_is_deduplicated_without_new_delivery(tmp_path):
    key = hashlib.sha256((str(tmp_path) + "duplicate").encode()).hexdigest()
    first = tmp_path / "first.txt"
    first.write_text("one", encoding="utf-8")
    accepted = bot_mode_dm._write_delivery_receipt(
        str(first), origin_session_id="origin-exact", label="@researcher",
        origin_reason="agent_session_fallback", idempotency_key=key,
    )
    second = tmp_path / "second.txt"
    second.write_text("one", encoding="utf-8")
    duplicate = bot_mode_dm._write_delivery_receipt(
        str(second), origin_session_id="origin-exact", label="@researcher",
        origin_reason="agent_session_fallback", idempotency_key=key,
    )
    assert duplicate["duplicate"] is True
    assert duplicate["delivery_id"] == accepted["delivery_id"]
    assert duplicate["origin_reason"] == "agent_session_fallback"


def test_concurrent_retry_serializes_one_delivery_id(tmp_path):
    key = hashlib.sha256((str(tmp_path) + "concurrent").encode()).hexdigest()
    def create(name):
        dm_file = tmp_path / (name + ".txt")
        dm_file.write_text("same", encoding="utf-8")
        return bot_mode_dm._write_delivery_receipt(
            str(dm_file), origin_session_id="origin-exact", origin_reason="explicit",
            label="@researcher", idempotency_key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(create, ("left", "right")))
    assert len({receipt["delivery_id"] for receipt in receipts}) == 1
    assert sum(bool(receipt.get("duplicate")) for receipt in receipts) == 1
