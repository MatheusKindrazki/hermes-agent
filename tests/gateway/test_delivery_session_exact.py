"""Delivery acknowledgement must be identity-bearing, never a bare PID."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sys
import types

from tools import bot_mode_dm


def test_delivery_receipt_binds_origin_session_and_reaches_terminal_state(tmp_path):
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("status", encoding="utf-8")
    accepted = bot_mode_dm._write_delivery_receipt(
        str(dm_file), origin_session_id="origin-exact", label="@researcher",
        origin_reason="explicit", route_reason="origin_exact",
        idempotency_key=hashlib.sha256(str(tmp_path).encode()).hexdigest(),
    )
    assert accepted["state"] == "accepted"
    assert accepted["origin_session_id"] == "origin-exact"
    assert accepted["origin_reason"] == "explicit"
    assert accepted["route_reason"] == "origin_exact"
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
        origin_reason="agent_session_fallback", route_reason="canonical_title_fallback", idempotency_key=key,
    )
    second = tmp_path / "second.txt"
    second.write_text("one", encoding="utf-8")
    duplicate = bot_mode_dm._write_delivery_receipt(
        str(second), origin_session_id="origin-exact", label="@researcher",
        origin_reason="agent_session_fallback", route_reason="canonical_title_fallback", idempotency_key=key,
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
            str(dm_file), origin_session_id="origin-exact", origin_reason="explicit", route_reason="origin_exact",
            label="@researcher", idempotency_key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(create, ("left", "right")))
    assert len({receipt["delivery_id"] for receipt in receipts}) == 1
    assert sum(bool(receipt.get("duplicate")) for receipt in receipts) == 1


def test_explicit_origin_routes_to_compression_tip_not_duplicate_title(monkeypatch, tmp_path):
    class DB:
        def __init__(self, **_kwargs): pass
        def get_session(self, session_id):
            return {"id": session_id, "title": "Bot Chat"} if session_id == "origin" else None
        def get_compression_tip(self, _session_id): return "origin-tip"
        def close(self): pass

    import hermes_cli.profiles as profiles
    import hermes_state
    monkeypatch.setattr(profiles, "get_profile_dir", lambda _profile: tmp_path)
    monkeypatch.setattr(hermes_state, "SessionDB", DB)
    resolved, reason = bot_mode_dm._resolve_target_bot_session("researcher", "origin")
    assert (resolved, reason) == ("origin-tip", "origin_exact")
    command = bot_mode_dm._delivery_command(
        ["hermes", "-p", "researcher", "--resume", resolved, "chat", "-c", "Bot Chat"],
        str(tmp_path / "payload.txt"), stdin_file=False,
    )
    assert "--resume origin-tip" in command


def test_failed_pre_spawn_releases_claim_for_real_retry(monkeypatch, tmp_path):
    dm_file = tmp_path / "payload.txt"
    dm_file.write_text("payload", encoding="utf-8")
    key = hashlib.sha256((str(tmp_path) + "retry").encode()).hexdigest()
    bot_mode_dm._write_delivery_receipt(
        str(dm_file), origin_session_id="origin", origin_reason="explicit",
        route_reason="origin_exact", label="@researcher", idempotency_key=key,
    )
    terminal = types.ModuleType("tools.terminal_tool")
    terminal.terminal_tool = lambda *_args, **_kwargs: '{"error":"spawn refused"}'
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", terminal)
    result = json.loads(bot_mode_dm._spawn_delivery("ignored", "@researcher", dm_file=str(dm_file), task_id=None, agent=None))
    assert "error" in result
    retry_file = tmp_path / "retry.txt"
    retry_file.write_text("payload", encoding="utf-8")
    retry = bot_mode_dm._write_delivery_receipt(
        str(retry_file), origin_session_id="origin", origin_reason="explicit",
        route_reason="origin_exact", label="@researcher", idempotency_key=key,
    )
    assert retry.get("duplicate") is not True


def test_terminal_delivery_emits_content_free_shadow_receipt(monkeypatch, tmp_path):
    dm_file = tmp_path / "payload.txt"
    dm_file.write_text("private message")
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "1")
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("HERMES_KERNEL_WORK_ID", "01a06c45-68ef-7d98-92cb-7225914a3437")
    monkeypatch.setenv("HERMES_KERNEL_AUTHORITY_VERSION", "4")
    monkeypatch.setenv("HERMES_KERNEL_TENANT", "personal")
    monkeypatch.setenv("HERMES_KERNEL_PROFILE", "kindra")
    monkeypatch.setenv("HERMES_KERNEL_ATTEMPT_ID", "01a06c46-68ef-7d98-92cb-7225914a3437")
    monkeypatch.setenv("HERMES_KERNEL_LEASE_EPOCH", "8")
    monkeypatch.setenv("HERMES_KERNEL_FENCE_VALIDATED", "1")
    receipt = bot_mode_dm._write_delivery_receipt(
        str(dm_file), origin_session_id="origin-exact", origin_reason="explicit",
        route_reason="origin_exact", label="@researcher",
        idempotency_key=hashlib.sha256(str(tmp_path).encode()).hexdigest(),
    )
    bot_mode_dm._update_delivery_receipt(str(dm_file), "delivered")
    events = list((tmp_path / "shadow").glob("*.json"))
    assert len(events) == 1
    event = json.loads(events[0].read_text())
    assert event["delivery_id"] == receipt["delivery_id"]
    assert event["fence_valid"] is True
    serialized = json.dumps(event)
    assert "private message" not in serialized and "message" not in event
