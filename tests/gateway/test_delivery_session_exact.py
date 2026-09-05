"""Delivery acknowledgement must be identity-bearing, never a bare PID."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sys
import types
from types import SimpleNamespace
from pathlib import Path

import pytest

from tools import bot_mode_dm


def _identity():
    return {
        "schema_version": "hermes.kernel-turn-identity.v1",
        "work_id": "01a061a7-cea0-7503-b308-1f4029d450c8", "authority_version": 4,
        "tenant": "personal", "profile": "default", "source": "default",
        "origin_session_id": "origin-exact", "origin_machine": "mac-mini",
        "source_event_id": "evt-4242", "source_event_observed_at": 1,
        "request_key": "request-key-4242",
        "attempt_id": "01a061a7-cea0-7503-b308-1f4029d450c9", "generation": 7,
        "authority_source": "remote", "authority_root_id": "a" * 32,
        "attestation": {"alg": "hmac-sha256", "version": "hermes-kernel-turn-identity-attestation.v1",
                        "key_id": "b" * 16, "value": "c" * 64},
    }


def _ack(delivery_id, request_key, identity_sha):
    return {
        "schema": "hermes.delivery-ack.v1", "delivery_id": delivery_id,
        "request_key": request_key, "turn_identity_sha256": identity_sha,
        "target_session_id": "target-session", "target_message_id": 42,
        "adapter": "cli", "state": "persisted", "accepted_at": 10,
        "persisted_at": 11,
    }


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


def test_global_environment_and_exit_code_cannot_emit_shadow_receipt(monkeypatch, tmp_path):
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
    assert list((tmp_path / "shadow").glob("*.json")) == []


def test_delivery_shadow_receipt_uses_authenticated_identity_source(monkeypatch, tmp_path):
    identity = _identity()
    record = {
        "delivery_id": "delivery-source-1",
        "origin_session_id": identity["origin_session_id"],
        "turn_identity": identity,
    }
    fence = {"fence_valid": True}
    spool = tmp_path / "shadow"
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "1")
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", str(spool))
    monkeypatch.setenv("HERMES_KERNEL_SOURCE", "environment-must-not-win")
    monkeypatch.setattr(bot_mode_dm, "_revalidate_delivery_identity", lambda _record: fence)

    bot_mode_dm._emit_delivery_shadow_receipt(record, "delivered", ack_bytes="{}")

    persisted = json.loads(next(spool.glob("*.json")).read_text(encoding="utf-8"))
    assert persisted["source"] == identity["source"]


def test_delivery_shadow_receipt_without_identity_source_is_not_published(monkeypatch, tmp_path):
    identity = _identity()
    identity.pop("source")
    record = {
        "delivery_id": "delivery-source-missing",
        "origin_session_id": identity["origin_session_id"],
        "turn_identity": identity,
    }
    spool = tmp_path / "shadow"
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "1")
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", str(spool))
    monkeypatch.setattr(
        bot_mode_dm,
        "_revalidate_delivery_identity",
        lambda _record: {"fence_valid": True},
    )

    bot_mode_dm._emit_delivery_shadow_receipt(record, "delivered", ack_bytes="{}")

    assert not list(spool.glob("*.json"))


def test_structured_ack_not_exit_code_releases_delivery_shadow(monkeypatch, tmp_path):
    dm_file = tmp_path / "payload.txt"
    dm_file.write_text("private message")
    identity = _identity()
    fence = {"schema_version": "hermes.kernel-turn-fence.v1", "fence_valid": True,
             "work_id": identity["work_id"], "attempt_id": identity["attempt_id"],
             "generation": 7, "identity_sha256": bot_mode_dm._canonical_sha256(identity),
             "checked_at": 10}
    monkeypatch.setattr(bot_mode_dm, "_delivery_ledger_path", lambda key: tmp_path / (key + ".json"))
    receipt = bot_mode_dm._write_delivery_receipt(
        str(dm_file), origin_session_id="origin-exact", origin_reason="explicit",
        route_reason="origin_exact", label="@researcher", idempotency_key="d" * 64,
        turn_identity=identity, fence=fence,
    )
    ack = _ack(receipt["delivery_id"], identity["request_key"], fence["identity_sha256"])
    ack["accepted_at"] = receipt["accepted_at"]
    proc = SimpleNamespace(returncode=0, stdout=json.dumps({"reply": "ok", "session_id": "target-session", "delivery_ack": ack}), stderr="")
    monkeypatch.setattr(bot_mode_dm.subprocess, "run", lambda *a, **k: proc)
    monkeypatch.setattr(bot_mode_dm, "_revalidate_delivery_identity", lambda record: fence)
    monkeypatch.setattr(bot_mode_dm, "_validate_local_ack_against_destination", lambda *a, **k: None)
    emitted = []
    monkeypatch.setattr(bot_mode_dm, "_emit_delivery_shadow_receipt", lambda record, state, ack_bytes=None: emitted.append((record, state, ack_bytes)))

    assert bot_mode_dm._run_delivery(["hermes", "-p", "researcher", "chat"], str(dm_file), stdin_file=False) == 0
    assert emitted and emitted[0][1] == "delivered"
    assert json.loads(emitted[0][2])["target_message_id"] == 42


@pytest.mark.parametrize("payload", [{}, {"delivery_ack": {"schema": "wrong"}}])
def test_missing_or_malformed_ack_never_marks_delivered(monkeypatch, tmp_path, payload):
    dm_file = tmp_path / "payload.txt"
    dm_file.write_text("private message")
    identity = _identity()
    fence = {"schema_version": "hermes.kernel-turn-fence.v1", "fence_valid": True,
             "work_id": identity["work_id"], "attempt_id": identity["attempt_id"],
             "generation": 7, "identity_sha256": bot_mode_dm._canonical_sha256(identity), "checked_at": 10}
    monkeypatch.setattr(bot_mode_dm, "_delivery_ledger_path", lambda key: tmp_path / (key + ".json"))
    bot_mode_dm._write_delivery_receipt(
        str(dm_file), origin_session_id="origin-exact", origin_reason="explicit",
        route_reason="origin_exact", label="@researcher", idempotency_key="e" * 64,
        turn_identity=identity, fence=fence,
    )
    monkeypatch.setattr(bot_mode_dm, "_revalidate_delivery_identity", lambda record: fence)
    monkeypatch.setattr(bot_mode_dm.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""))
    monkeypatch.setattr(bot_mode_dm, "_emit_delivery_shadow_receipt", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no shadow")))
    assert bot_mode_dm._run_delivery(["hermes"], str(dm_file), stdin_file=False) != 0


def test_stale_fence_stops_before_transport(monkeypatch, tmp_path):
    dm_file = tmp_path / "payload.txt"
    dm_file.write_text("private message")
    identity = _identity()
    fence = {"schema_version": "hermes.kernel-turn-fence.v1", "fence_valid": True,
             "work_id": identity["work_id"], "attempt_id": identity["attempt_id"],
             "generation": 7, "identity_sha256": bot_mode_dm._canonical_sha256(identity), "checked_at": 10}
    monkeypatch.setattr(bot_mode_dm, "_delivery_ledger_path", lambda key: tmp_path / (key + ".json"))
    bot_mode_dm._write_delivery_receipt(
        str(dm_file), origin_session_id="origin-exact", origin_reason="explicit",
        route_reason="origin_exact", label="@researcher", idempotency_key="f" * 64,
        turn_identity=identity, fence=fence,
    )
    monkeypatch.setattr(bot_mode_dm, "_revalidate_delivery_identity", lambda record: (_ for _ in ()).throw(RuntimeError("stale")))
    monkeypatch.setattr(bot_mode_dm.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("transport ran")))
    assert bot_mode_dm._run_delivery(["hermes"], str(dm_file), stdin_file=False) == 1


def test_local_receiver_uses_real_sessiondb_row_for_idempotent_ack(tmp_path):
    import cli
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    session_id = db.create_session("target", "cli")
    identity = dict(_identity(), request_key="request-local-1")
    envelope = {"schema": "hermes.delivery-envelope.v1", "delivery_id": "delivery-local-1",
                "request_key": "request-local-1", "turn_identity_sha256": bot_mode_dm._canonical_sha256(identity),
                "accepted_at": 10}
    encoded = bot_mode_dm._encode_delivery_query(envelope, "private")
    message, decoded = cli._decode_delivery_query(encoded)
    assert message == "private" and decoded == envelope
    row_id = db.append_message(
        session_id, "user", message, display_kind="bot_delivery",
        display_metadata=cli._delivery_display_metadata(decoded),
    )
    first = cli._delivery_ack(db, session_id, decoded, adapter="cli")
    second = cli._delivery_ack(db, session_id, decoded, adapter="cli")
    assert first == second
    assert first["target_message_id"] == row_id
    assert first["target_session_id"] == session_id
    bot_mode_dm._validate_local_ack_row(
        db, first,
        {"delivery_id": envelope["delivery_id"], "accepted_at": 10, "turn_identity": identity},
    )
    with pytest.raises(cli.DeliveryRebindError):
        cli._delivery_ack(
            db, session_id, dict(envelope, request_key="request-local-rebound"), adapter="cli"
        )
    assert sum(1 for row in db.get_messages(session_id) if row.get("role") == "user") == 1


def test_sender_rejects_ack_rebound_to_response_session():
    identity = _identity()
    record = {"delivery_id": "delivery-1", "accepted_at": 10, "turn_identity": identity}
    ack = _ack("delivery-1", identity["request_key"], bot_mode_dm._canonical_sha256(identity))
    payload = {"session_id": "different-session", "delivery_ack": ack}
    with pytest.raises(ValueError, match="session"):
        bot_mode_dm._validated_delivery_ack(payload, record)


def test_sender_rejects_nonexistent_local_target_message(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    session_id = db.create_session("target", "cli")
    identity = _identity()
    record = {"delivery_id": "delivery-1", "accepted_at": 10, "turn_identity": identity}
    ack = _ack("delivery-1", identity["request_key"], bot_mode_dm._canonical_sha256(identity))
    ack.update({"target_session_id": session_id, "target_message_id": 999})
    with pytest.raises(ValueError, match="message"):
        bot_mode_dm._validate_local_ack_row(db, ack, record)


def test_truncated_ledger_fails_closed_without_new_delivery_id(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{truncated", encoding="utf-8")
    ledger.chmod(0o600)
    monkeypatch.setattr(bot_mode_dm, "_delivery_ledger_path", lambda _key: ledger)
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("one", encoding="utf-8")
    with pytest.raises(RuntimeError, match="ledger"):
        bot_mode_dm._write_delivery_receipt(
            str(dm_file), origin_session_id="origin", origin_reason="explicit",
            route_reason="origin_exact", label="@target", idempotency_key="a" * 64,
        )
    assert not Path(str(dm_file) + ".receipt.json").exists()


def test_orphaned_lock_file_is_recoverable(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.json"
    lock = ledger.with_suffix(".lock")
    lock.write_text("orphan", encoding="utf-8")
    lock.chmod(0o600)
    monkeypatch.setattr(bot_mode_dm, "_delivery_ledger_path", lambda _key: ledger)
    with bot_mode_dm._delivery_ledger_lock("a" * 64):
        assert lock.exists()
