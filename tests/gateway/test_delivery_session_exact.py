"""Delivery acknowledgement must be identity-bearing, never a bare PID."""

from __future__ import annotations

import json
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sys
import types
from types import SimpleNamespace
from pathlib import Path

import pytest

from tools import bot_mode_dm


@pytest.mark.parametrize("failure", [None, "release_ambiguous", "spool_failed", "ack_missing"])
def test_observe_effect_four_stores_exact_release_and_ack_replay(tmp_path, monkeypatch, failure):
    """Real SessionDB, private ledger/spool and POS collector SQLite.

    Admission/release transport responses are explicit offline fixtures, not
    evidence of a live Jarvis lifecycle event or production authority.
    """
    import cli
    import importlib.util
    import os
    import time
    from agent import durable_admission as da
    from hermes_state import SessionDB

    root = Path(os.environ.get("HERMES_TEST_K7_ROOT",
        "/Users/matheuskindrazki/development/personal/.worktrees/hermes-personal-os/kindra-passive-observer-20260906"))
    spec = importlib.util.spec_from_file_location("observation_collector", root / "cron/scripts/kernel-shadow-collector.py")
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    spool = tmp_path / "spool"
    writer = da.ObservationWriter(spool, code_sha="a" * 40)
    monkeypatch.setattr(da, "_OBSERVER", writer)
    monkeypatch.setenv(da.ENV_MODE, "observe")
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "1")
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", str(spool))
    monkeypatch.setattr(da, "_settings", lambda: {"tenant": "personal", "machine": "mini"})
    monkeypatch.setattr(da, "_active_profile", lambda: "projetospessoais")
    monkeypatch.setattr(bot_mode_dm.tempfile, "gettempdir", lambda: str(tmp_path))
    identity = dict(_identity(), profile="projetospessoais", source="projetospessoais", origin_machine="mini")
    fence = {"fence_valid": True, "identity_sha256": bot_mode_dm._canonical_sha256(identity)}
    writer.checkpoint()
    receiver = SessionDB(tmp_path / "receiver.db")
    target_session = receiver.create_session("receiver-target", "cli")
    sends, releases, fences = [], [], []
    monkeypatch.setattr(da, "admit_observed_effect", lambda obs: identity)
    monkeypatch.setattr(da, "_run_turn_identity_revalidation", lambda value: fences.append(value) or fence)
    monkeypatch.setattr(bot_mode_dm, "_delivery_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(bot_mode_dm, "_validate_local_ack_against_destination",
        lambda ack, record, argv: bot_mode_dm._validate_local_ack_row(receiver, ack, record))
    if failure == "ack_missing":
        monkeypatch.setattr(bot_mode_dm, "_validate_local_ack_against_destination",
            lambda *a: (_ for _ in ()).throw(OSError("independent SessionDB unavailable")))
    if failure == "spool_failed":
        monkeypatch.setattr(bot_mode_dm, "_emit_delivery_shadow_receipt",
            lambda *a, **k: (_ for _ in ()).throw(OSError("spool unavailable")))

    def transport(argv, **kwargs):
        query = Path(argv[argv.index("--query-file") + 1])
        message, envelope = cli._decode_delivery_query(query.read_text())
        receiver.append_message(target_session, "user", message, display_kind="bot_delivery",
            display_metadata=cli._delivery_display_metadata(envelope))
        ack = cli._delivery_ack(receiver, target_session, envelope, adapter="cli")
        sends.append(ack)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"reply": "ok", "session_id": target_session, "delivery_ack": ack}), stderr="")

    def release(value, event_id):
        releases.append(event_id)
        if failure == "release_ambiguous":
            raise TimeoutError("ambiguous transport")
        session = "hk1-" + hashlib.sha256(json.dumps(["hermes-kernel-execution.v1", value["work_id"], value["origin_session_id"]], separators=(",", ":")).encode()).hexdigest()
        source = "kernel-effect-" + hashlib.sha256(json.dumps(["hermes-kernel-effect-release.v1", value["work_id"], value["attempt_id"], value["generation"], session, event_id], separators=(",", ":")).encode()).hexdigest() + ":release-" + str(value["generation"])
        return {"schema_version": "work-control.effect-release.v1", "contract_version": "work-control.v1",
            "outcome": "released", "work_id": value["work_id"], "origin_session_id": value["origin_session_id"],
            "attempt_id": value["attempt_id"], "generation": value["generation"], "release_event_id": event_id,
            "execution_session_key": session, "source_event_id": source}

    monkeypatch.setattr(da, "release_observed_effect", release)
    monkeypatch.setattr(bot_mode_dm.subprocess, "run", transport)
    receipts = []
    def spawn(command, label, **kwargs):
        receipts.append(kwargs["dm_file"])
        assert bot_mode_dm._run_delivery(["hermes", "-p", "receiver"], kwargs["dm_file"], stdin_file=False) == 0
        return json.dumps({"status": "accepted"})
    monkeypatch.setattr(bot_mode_dm, "_spawn_delivery", spawn)
    agent = SimpleNamespace(session_id="wrong-cached-origin", _session_db=SimpleNamespace(db_path=str(tmp_path / "sender.db")))
    origin = da.capture_effect_origin("origin-exact", "telegram:123")
    with da.effect_origin_scope(origin):
        bot_mode_dm._start_delivery(["hermes", "-p", "receiver"], "private outgoing", "receiver",
            stdin_file=False, task_id=None, agent=agent)
        replay = json.loads(bot_mode_dm._start_delivery(["hermes", "-p", "receiver"], "private outgoing", "receiver",
            stdin_file=False, task_id=None, agent=agent))
    if failure != "ack_missing":
        bot_mode_dm._update_delivery_receipt(receipts[0], "delivered", ack=sends[0], ack_row_validated=True)
        assert bot_mode_dm._run_delivery(["hermes", "-p", "receiver"], receipts[0], stdin_file=False) == 0
    assert replay["status"] == ("accepted" if failure == "ack_missing" else "delivered")
    assert len(sends) == 1 and len(fences) == 2
    assert len(releases) == (0 if failure in {"spool_failed", "ack_missing"} else 1)
    assert len(receiver.get_messages(target_session)) == 1
    effect = json.loads(next((spool / "observation-effects").glob("*.json")).read_text())
    assert effect["sequence"] == 1
    ledger_record = json.loads(Path(receipts[0] + ".receipt.json").read_text())
    if releases:
        assert releases == [ledger_record["release_event_id"]]
    if failure is not None:
        assert effect["state"] == "gap" and effect["gap_reason"] == failure
        if failure == "release_ambiguous":
            assert ledger_record["release_state"] == "requested"
        receiver.close()
        return
    assert effect["state"] == "settled"
    assert ledger_record["release_state"] == "acknowledged"
    settlement = effect["settlement"]
    ledger_path = bot_mode_dm._delivery_ledger_path(ledger_record["idempotency_key"])
    assert settlement["ledger_sha256"] == hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    collector_db = collector._connect(tmp_path / "collector.db")
    assert collector._ingest(collector_db, spool, {"max_receipt_bytes": 65536}, int(time.time())) == (1, 0, 0)
    assert collector._ingest(collector_db, spool, {"max_receipt_bytes": 65536}, int(time.time())) == (0, 1, 0)
    assert collector._settlement_valid(effect)
    # A callback fixture is insufficient: no canonical Jarvis release exported.
    assert not collector._settlement_correlated(collector_db, effect)
    assert collector_db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    # Explicit offline projection fixture: proves the collector correlation,
    # not that Jarvis emitted it in a live installation.
    from gateway.turn_context import write_kernel_shadow_receipt
    delivery_event = json.loads((spool / (settlement["delivery_event_id"] + ".json")).read_text())
    release_projection_id = "01a061a7-cea0-7503-b308-1f4029d450cb"
    release_projection = dict(delivery_event, event_id=release_projection_id,
        source_event_id=release_projection_id, action="terminal", outcome="unknown",
        delivery_id="", terminal=False, adapter_receipt_sha256="f" * 64)
    write_kernel_shadow_receipt(release_projection)
    assert collector._ingest(collector_db, spool, {"max_receipt_bytes": 65536}, int(time.time())) == (1, 1, 0)
    assert collector._settlement_correlated(collector_db, effect)
    assert collector._observation_records(collector_db, spool, {"max_receipt_bytes": 65536}, int(time.time())) is None
    assert collector_db.execute("SELECT COUNT(*) FROM observation_effects").fetchone()[0] == 1
    collector_db.close()
    receiver.close()


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


@pytest.mark.parametrize("fault", [None, "generation", "source_event_id", "extra", "timeout"])
def test_exact_release_pinned_cli_closed_wire_single_call(tmp_path, monkeypatch, fault):
    from agent import durable_admission as da
    import subprocess

    identity = dict(_identity(), profile="projetospessoais", source="projetospessoais", origin_machine="mini")
    event_id = "01a061a7-cea0-7503-b308-1f4029d450ca"
    response = {"schema_version": "work-control.effect-release.v1", "contract_version": "work-control.v1",
        "outcome": "released", "work_id": identity["work_id"], "origin_session_id": "origin-exact",
        "attempt_id": identity["attempt_id"], "generation": 7, "release_event_id": event_id,
        "execution_session_key": "hk1-a8a115423c86c374acbd53e76d5510ca2f214944ca3ee48abaaa3ee52e14d4fd",
        "source_event_id": "kernel-effect-6e2f536d661d670096b35a0b743adc66b5f53937400a69d351a3054d7e7a7b57:release-7"}
    if fault == "generation":
        response["generation"] = 8
    if fault == "source_event_id":
        response["source_event_id"] = "foreign-release"
    if fault == "extra":
        response["text"] = "forbidden"
    monkeypatch.setattr(da, "_resolve_trust_roots", lambda: SimpleNamespace(admitter_bin="/fixture/pinned-cli", root_dir=tmp_path))
    monkeypatch.setattr(da, "_settings", lambda: {"jarvis_base_url": "https://fixture.invalid", "secret_ref": "fixture-ref"})
    calls = []
    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 2)
        return SimpleNamespace(returncode=0, stdout=json.dumps(response))
    monkeypatch.setattr(da.subprocess, "run", execute)
    if fault:
        with pytest.raises((da.TurnIdentityError, subprocess.TimeoutExpired)):
            da.release_observed_effect(identity, event_id)
    else:
        assert da.release_observed_effect(identity, event_id) == response
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv == ["/fixture/pinned-cli", "release-turn-identity", "--stdin", "--jarvis-base-url", "https://fixture.invalid", "--secret-ref", "fixture-ref"]
    assert json.loads(options["input"]) == {"turn_identity": identity, "release_event_id": event_id}
    assert options["timeout"] == 2 and options["shell"] is False


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


def test_real_sender_converges_receiver_ack_sidecar_and_one_shadow_delivery(monkeypatch, tmp_path):
    """The rollout harness must execute the sender, not stop at receiver ACK."""
    import cli
    from hermes_state import SessionDB

    identity = _identity()
    fence = {
        "schema_version": "hermes.kernel-turn-fence.v1",
        "fence_valid": True,
        "work_id": identity["work_id"],
        "attempt_id": identity["attempt_id"],
        "generation": identity["generation"],
        "identity_sha256": bot_mode_dm._canonical_sha256(identity),
        "checked_at": 10,
    }
    spool = tmp_path / "shadow"
    ledger = tmp_path / "ledger.json"
    receiver = SessionDB(tmp_path / "receiver.db")
    target_session = receiver.create_session("sender-harness-target", "cli")
    transport_calls = 0
    issued_acks = []

    monkeypatch.setenv("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "1")
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", str(spool))
    monkeypatch.setattr(bot_mode_dm, "_delivery_ledger_path", lambda _key: ledger)
    monkeypatch.setattr(bot_mode_dm, "_delivery_lock", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(bot_mode_dm, "_revalidate_delivery_identity", lambda _record: fence)
    monkeypatch.setattr(
        bot_mode_dm,
        "_validate_local_ack_against_destination",
        lambda ack, record, _argv: bot_mode_dm._validate_local_ack_row(receiver, ack, record),
    )

    def receiver_transport(argv, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        query_path = Path(argv[argv.index("--query-file") + 1])
        message, envelope = cli._decode_delivery_query(query_path.read_text(encoding="utf-8"))
        assert envelope is not None
        if cli._delivery_ack(receiver, target_session, envelope, adapter="cli") is None:
            receiver.append_message(
                target_session,
                "user",
                message,
                display_kind="bot_delivery",
                display_metadata=cli._delivery_display_metadata(envelope),
            )
        ack = cli._delivery_ack(receiver, target_session, envelope, adapter="cli")
        assert ack is not None
        issued_acks.append(dict(ack))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"reply": "ok", "session_id": target_session, "delivery_ack": ack}),
            stderr="",
        )

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", receiver_transport)
    idempotency_key = "7" * 64
    first_file = tmp_path / "first.txt"
    first_file.write_text("internal status", encoding="utf-8")
    accepted = bot_mode_dm._write_delivery_receipt(
        str(first_file),
        origin_session_id=identity["origin_session_id"],
        origin_reason="explicit",
        route_reason="origin_exact",
        label="mini/default",
        idempotency_key=idempotency_key,
        turn_identity=identity,
        fence=fence,
    )

    assert bot_mode_dm._run_delivery(
        ["hermes", "-p", "default", "chat"], str(first_file), stdin_file=False
    ) == 0
    persisted = json.loads((tmp_path / "first.txt.receipt.json").read_text(encoding="utf-8"))
    issued_ack = issued_acks[0]
    assert persisted["adapter_ack"] == issued_ack
    assert bot_mode_dm._validated_delivery_ack(
        {"session_id": target_session, "delivery_ack": issued_ack},
        persisted,
        expected_adapter="cli",
    ) == issued_ack
    assert persisted["state"] == "delivered"
    assert persisted["adapter_receipt_sha256"] == bot_mode_dm._canonical_sha256(issued_ack)

    events = list(spool.glob("*.json"))
    assert len(events) == 1
    event = json.loads(events[0].read_text(encoding="utf-8"))
    assert event["schema"] == "hermes.kernel-shadow-event/v1"
    assert event["action"] == "delivery"
    assert event["source"] == identity["source"]
    assert event["delivery_id"] == accepted["delivery_id"]
    assert event["adapter_receipt_sha256"] == hashlib.sha256(
        bot_mode_dm._canonical(issued_ack).encode("utf-8")
    ).hexdigest()

    replay_file = tmp_path / "replay.txt"
    replay_file.write_text("internal status", encoding="utf-8")
    replay = bot_mode_dm._write_delivery_receipt(
        str(replay_file),
        origin_session_id=identity["origin_session_id"],
        origin_reason="explicit",
        route_reason="origin_exact",
        label="mini/default",
        idempotency_key=idempotency_key,
        turn_identity=identity,
        fence=fence,
    )
    assert replay["duplicate"] is True
    assert replay["delivery_id"] == accepted["delivery_id"]
    assert bot_mode_dm._run_delivery(
        ["hermes", "-p", "default", "chat"], str(replay_file), stdin_file=False
    ) == 0
    replay_sidecar = json.loads(
        (tmp_path / "replay.txt.receipt.json").read_text(encoding="utf-8")
    )
    assert replay_sidecar["delivery_id"] == persisted["delivery_id"]
    assert issued_acks[1] == issued_ack
    assert replay_sidecar["adapter_ack"] == issued_ack
    assert replay_sidecar["adapter_ack"]["target_session_id"] == issued_ack["target_session_id"]
    assert replay_sidecar["adapter_ack"]["target_message_id"] == issued_ack["target_message_id"]
    assert len(list(spool.glob("*.json"))) == 1
    assert transport_calls == 2
    assert sum(
        row.get("role") == "user" for row in receiver.get_messages(target_session)
    ) == 1

    # Receiver persistence alone is deliberately not a shadow producer.
    isolated_envelope = dict(
        bot_mode_dm._delivery_envelope_from_record(persisted),
        delivery_id="receiver-only-delivery",
    )
    isolated_session = receiver.create_session("receiver-only", "cli")
    receiver.append_message(
        isolated_session,
        "user",
        "receiver only",
        display_kind="bot_delivery",
        display_metadata=cli._delivery_display_metadata(isolated_envelope),
    )
    assert cli._delivery_ack(
        receiver, isolated_session, isolated_envelope, adapter="cli"
    )["state"] == "persisted"
    assert len(list(spool.glob("*.json"))) == 1
    receiver.close()


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
