"""K6 regression: durable turns while >1M-token compression is blocked.

The payloads stay tiny. ``token_count=1_000_001`` is synthetic telemetry, not
an allocation of a million-token transcript.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.conversation_compression import (
    DurableCompressionTurnSpool,
    drain_compression_turns_into_agent,
    ensure_compression_turn_identity,
)
from agent.conversation_loop import _compression_deferred_result
from hermes_state import SessionDB


def _turn(content: str) -> dict:
    return {"role": "user", "content": content}


def test_blocked_compressor_two_turns_are_durable_ordered_and_exactly_once(
    tmp_path,
):
    spool = DurableCompressionTurnSpool(tmp_path / "compression-spool")
    parent = "session-parent"
    owner = "compressor-a"
    epoch = spool.begin_attempt(parent, owner)
    assert epoch == 1
    assert spool.begin_attempt(parent, "compressor-b") is None

    first_done = threading.Event()
    receipts = []

    def enqueue_first():
        receipts.append(
            spool.enqueue(
                parent,
                _turn("first"),
                token_count=1_000_001,
                client_turn_id="turn-1",
            )
        )
        first_done.set()

    def enqueue_second():
        assert first_done.wait(timeout=5)
        receipts.append(
            spool.enqueue(
                parent,
                _turn("second"),
                token_count=1_000_001,
                client_turn_id="turn-2",
            )
        )

    threads = [threading.Thread(target=enqueue_first), threading.Thread(target=enqueue_second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert [receipt["accepted"] for receipt in receipts] == [True, True]
    assert all(receipt["durable"] for receipt in receipts)
    assert [row["message"]["content"] for row in spool.pending(parent)] == [
        "first",
        "second",
    ]

    assert spool.commit_attempt(parent, owner, epoch, live_tip="session-child") is True
    # A detached/stale compressor can never repoint the lineage after commit.
    assert spool.commit_attempt(parent, owner, epoch - 1, live_tip="stale-child") is False
    child_epoch = spool.begin_attempt("session-child", "compressor-c")
    assert child_epoch == 1
    assert spool.commit_attempt(
        "session-child", "compressor-c", child_epoch, live_tip="session-grandchild"
    ) is True
    assert spool.resolve_live_tip(parent) == "session-grandchild"

    seen = []
    drained = spool.drain(parent, lambda row, tip: seen.append((row["message"]["content"], tip)))
    assert drained == ["turn-1", "turn-2"]
    assert seen == [
        ("first", "session-grandchild"),
        ("second", "session-grandchild"),
    ]
    assert spool.drain(parent, lambda *_: None) == []

    # Entries remain as an audit trail; drain marks them instead of deleting.
    records = spool.records(parent)
    assert [record["status"] for record in records] == ["drained", "drained"]


def test_restart_after_kill_between_spool_and_drain_resumes_without_duplicate(tmp_path):
    root = tmp_path / "compression-spool"
    first_process = DurableCompressionTurnSpool(root)
    receipt = first_process.enqueue(
        "session-a",
        _turn("survives kill"),
        token_count=1_000_001,
        client_turn_id="turn-kill",
    )
    assert receipt["durable"] is True

    # Simulated kill: discard all in-memory state before any drain.
    restarted = DurableCompressionTurnSpool(root)
    seen = []
    assert restarted.drain(
        "session-a", lambda row, tip: seen.append((row["message"]["content"], tip))
    ) == ["turn-kill"]
    assert seen == [("survives kill", "session-a")]

    second_restart = DurableCompressionTurnSpool(root)
    assert second_restart.drain("session-a", lambda *_: None) == []


def test_legacy_spool_record_remains_readable_and_is_never_deleted(tmp_path):
    root = tmp_path / "compression-spool"
    root.mkdir()
    legacy = root / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "session_id": "legacy-session",
                "client_turn_id": "legacy-turn",
                "message": _turn("legacy"),
                "created_ns": 1,
            }
        ),
        encoding="utf-8",
    )

    spool = DurableCompressionTurnSpool(root)
    assert [row["message"]["content"] for row in spool.pending("legacy-session")] == [
        "legacy"
    ]
    assert spool.drain("legacy-session", lambda *_: None) == ["legacy-turn"]
    assert legacy.exists()
    assert json.loads(legacy.read_text(encoding="utf-8"))["status"] == "drained"


def test_lock_deferred_turn_is_durable_before_accepted_response(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = SimpleNamespace(
        session_id="busy-session",
        _compression_skipped_due_to_lock="owner",
        _flush_status_buffer=lambda: None,
    )
    result = _compression_deferred_result(
        agent,
        [_turn("accepted while busy")],
        0,
        token_count=1_000_001,
    )
    assert result["compression_deferred"] is True
    assert result["compression_handoff_accepted"] is True
    assert result["error"] is None
    assert "already running" not in result["final_response"].lower()
    pending = DurableCompressionTurnSpool(
        tmp_path / "compression_turn_spool"
    ).pending("busy-session")
    assert [row["message"]["content"] for row in pending] == [
        "accepted while busy"
    ]


def test_restart_dedupes_if_db_append_won_before_spool_mark(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spool = DurableCompressionTurnSpool(tmp_path / "compression_turn_spool")
    spool.enqueue(
        "parent",
        _turn("once"),
        token_count=1_000_001,
        client_turn_id="turn-once",
    )
    epoch = spool.begin_attempt("parent", "owner")
    assert spool.commit_attempt("parent", "owner", epoch, live_tip="child")

    class FakeDb:
        def __init__(self):
            self.ids = set()
            self.rows = []

        def has_platform_message_id(self, session_id, platform_message_id):
            return (session_id, platform_message_id) in self.ids

        def append_message(self, session_id, role, content, **kwargs):
            key = (session_id, kwargs["platform_message_id"])
            self.ids.add(key)
            self.rows.append((session_id, role, content, key[1]))

    db = FakeDb()
    agent = SimpleNamespace(_session_db=db)
    live = []
    assert drain_compression_turns_into_agent(agent, "parent", live) == ["turn-once"]
    assert len(db.rows) == 1

    # Simulate a kill after the DB append committed but before the spool's
    # drained marker became durable. Recovery consults the DB idempotency key.
    record_path = next((tmp_path / "compression_turn_spool").glob("turn-*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["status"] = "pending"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    restarted_live = []
    assert drain_compression_turns_into_agent(
        agent, "parent", restarted_live
    ) == ["turn-once"]
    assert len(db.rows) == 1
    assert [message["content"] for message in restarted_live] == ["once"]


def test_below_million_lock_defer_is_also_durably_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = SimpleNamespace(
        session_id="below-million",
        _compression_skipped_due_to_lock="owner",
        _flush_status_buffer=lambda: None,
    )
    result = _compression_deferred_result(
        agent, [_turn("small but contended")], 0, token_count=999
    )
    assert result["compression_handoff_accepted"] is True
    assert DurableCompressionTurnSpool(
        tmp_path / "compression_turn_spool"
    ).pending("below-million")


def test_permissions_are_private_even_with_umask_zero(tmp_path):
    old_umask = os.umask(0)
    try:
        root = tmp_path / "spool"
        spool = DurableCompressionTurnSpool(root)
        spool.enqueue("s", _turn("private"), token_count=1)
        assert spool.begin_attempt("s", "owner") == 1
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_symlink_root_and_lock_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)
    with __import__("pytest").raises(Exception):
        DurableCompressionTurnSpool(root_link).enqueue("s", _turn("x"), token_count=1)

    root = tmp_path / "spool"
    root.mkdir(mode=0o700)
    spool = DurableCompressionTurnSpool(root)
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    lock_path = root / f".lock-{spool._key('global')}"
    lock_path.symlink_to(victim)
    with __import__("pytest").raises(Exception):
        spool.enqueue("s", _turn("x"), token_count=1)
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_symlink_state_global_and_record_are_rejected(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")

    global_root = tmp_path / "global-root"
    global_root.mkdir(mode=0o700)
    global_spool = DurableCompressionTurnSpool(global_root)
    (global_root / "global-state.json").symlink_to(victim)
    with __import__("pytest").raises(Exception):
        global_spool.enqueue("s", _turn("x"), token_count=1)

    state_root = tmp_path / "state-root"
    state_root.mkdir(mode=0o700)
    state_spool = DurableCompressionTurnSpool(state_root)
    state_spool._state_path("s").symlink_to(victim)
    with __import__("pytest").raises(Exception):
        state_spool.begin_attempt("s", "owner")

    record_root = tmp_path / "record-root"
    record_root.mkdir(mode=0o700)
    record_spool = DurableCompressionTurnSpool(record_root)
    (record_root / "legacy.json").symlink_to(victim)
    with __import__("pytest").raises(Exception):
        record_spool.pending("s")
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_corrupt_json_is_explicit_and_preserved(tmp_path):
    root = tmp_path / "spool"
    spool = DurableCompressionTurnSpool(root)
    spool.enqueue("s", _turn("ok"), token_count=1)

    record = next(root.glob("turn-*.json"))
    record.write_bytes(b"{broken-record")
    with __import__("pytest").raises(Exception):
        spool.pending("s")
    assert record.read_bytes() == b"{broken-record"

    record.write_text(json.dumps({"message": _turn("ok"), "session_id": "s"}))
    state = spool._state_path("s")
    state.write_bytes(b"{broken-state")
    with __import__("pytest").raises(Exception):
        spool.begin_attempt("s", "owner")
    assert state.read_bytes() == b"{broken-state"

    state.unlink()
    global_state = root / "global-state.json"
    global_state.write_bytes(b"{broken-global")
    with __import__("pytest").raises(Exception):
        spool.enqueue("s", _turn("new"), token_count=1)
    assert global_state.read_bytes() == b"{broken-global"


def test_structurally_invalid_json_is_explicit_and_preserved(tmp_path):
    root = tmp_path / "spool"
    spool = DurableCompressionTurnSpool(root)
    spool.enqueue("s", _turn("ok"), token_count=1)

    record = next(root.glob("turn-*.json"))
    record.write_bytes(b"{}")
    with __import__("pytest").raises(RuntimeError, match="record schema"):
        spool.pending("s")
    assert record.read_bytes() == b"{}"
    record.unlink()

    state = spool._state_path("s")
    state.write_bytes(b"{}")
    with __import__("pytest").raises(RuntimeError, match="state schema"):
        spool.begin_attempt("s", "owner")
    assert state.read_bytes() == b"{}"
    state.unlink()

    global_state = root / "global-state.json"
    global_state.write_bytes(b"{}")
    with __import__("pytest").raises(RuntimeError, match="global schema"):
        spool.enqueue("s", _turn("new"), token_count=1)
    assert global_state.read_bytes() == b"{}"


def test_same_session_invalid_state_blocks_ack_without_cross_session_poison(tmp_path):
    root = tmp_path / "spool"
    spool = DurableCompressionTurnSpool(root)
    assert spool.enqueue(
        "healthy", _turn("first"), token_count=1, client_turn_id="healthy-1"
    )["durable"]
    invalid_state = spool._state_path("broken")
    invalid_state.write_bytes(b"{}")
    before = {path.name: path.read_bytes() for path in root.iterdir()}

    with pytest.raises(RuntimeError, match="state schema"):
        spool.enqueue(
            "broken", _turn("must not ack"), token_count=1,
            client_turn_id="broken-1",
        )
    assert invalid_state.read_bytes() == b"{}"
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before

    receipt = spool.enqueue(
        "healthy", _turn("isolated"), token_count=1,
        client_turn_id="healthy-2",
    )
    assert receipt["durable"] is True
    assert [row["client_turn_id"] for row in spool.pending("healthy")] == [
        "healthy-1", "healthy-2"
    ]


def test_root_swap_after_open_refuses_noncanonical_durable_ack(tmp_path, monkeypatch):
    root = tmp_path / "spool"
    external = tmp_path / "external"
    external.mkdir()
    spool = DurableCompressionTurnSpool(root)
    original = spool._open_root_fd
    swapped = False

    def swap_after_open():
        nonlocal swapped
        fd = original()
        if not swapped:
            swapped = True
            parked = tmp_path / "parked"
            root.rename(parked)
            root.symlink_to(external, target_is_directory=True)
        return fd

    monkeypatch.setattr(spool, "_open_root_fd", swap_after_open)
    with pytest.raises(RuntimeError, match="canonical|symlink|non-directory"):
        spool.enqueue("s", _turn("anchored"), token_count=1)
    assert list(external.iterdir()) == []


def test_intermediate_symlink_is_rejected_without_external_write(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(external, target_is_directory=True)
    spool = DurableCompressionTurnSpool(linked_home / "compression_turn_spool")
    with pytest.raises(RuntimeError, match="symlink|directory|canonical"):
        spool.enqueue("s", _turn("must stay out"), token_count=1)
    assert list(external.iterdir()) == []


def test_real_session_db_and_live_context_dedupe_original_message_id(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="test")
    db.append_message(
        "child",
        "user",
        "already durable",
        platform_message_id="original-message-id",
    )
    spool = DurableCompressionTurnSpool(tmp_path / "compression_turn_spool")
    spool.enqueue(
        "parent",
        {
            "role": "user",
            "content": "already durable",
            "message_id": "original-message-id",
        },
        token_count=1,
        client_turn_id="client-turn-id",
    )
    epoch = spool.begin_attempt("parent", "owner")
    assert spool.commit_attempt("parent", "owner", epoch, live_tip="child")

    live = db.get_messages_as_conversation("child")
    agent = SimpleNamespace(_session_db=db)
    assert drain_compression_turns_into_agent(agent, "parent", live) == [
        "client-turn-id"
    ]
    loaded = db.get_messages_as_conversation("child")
    assert [row["content"] for row in loaded].count("already durable") == 1
    assert [row["content"] for row in live].count("already durable") == 1

    # Crash window: durable DB row exists but drained mark is lost.
    record_path = next((tmp_path / "compression_turn_spool").glob("turn-*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["status"] = "pending"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    restarted_live = db.get_messages_as_conversation("child")
    assert drain_compression_turns_into_agent(
        agent, "parent", restarted_live
    ) == ["client-turn-id"]
    assert [
        row["content"] for row in db.get_messages_as_conversation("child")
    ].count("already durable") == 1


def test_two_identical_no_id_turns_remain_exactly_two_after_mark_crash(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="test")
    spool = DurableCompressionTurnSpool(tmp_path / "compression_turn_spool")
    turns = [_turn("same"), _turn("same")]
    ids = [ensure_compression_turn_identity(turn) for turn in turns]
    assert len(set(ids)) == 2
    for turn, turn_id in zip(turns, ids):
        db.append_message(
            "child", "user", turn["content"],
            display_metadata=turn["display_metadata"],
        )
        spool.enqueue("parent", turn, token_count=1, client_turn_id=turn_id)
    epoch = spool.begin_attempt("parent", "owner")
    assert spool.commit_attempt("parent", "owner", epoch, live_tip="child")

    agent = SimpleNamespace(_session_db=db)
    live = db.get_messages_as_conversation("child")
    drain_compression_turns_into_agent(agent, "parent", live)
    assert [row["content"] for row in db.get_messages_as_conversation("child")] == [
        "same", "same"
    ]

    # Simulate append -> durable drained-mark loss for both records.
    for path in (tmp_path / "compression_turn_spool").glob("turn-*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "pending"
        path.write_text(json.dumps(payload), encoding="utf-8")
    restarted_live = db.get_messages_as_conversation("child")
    drain_compression_turns_into_agent(agent, "parent", restarted_live)
    assert [row["content"] for row in db.get_messages_as_conversation("child")] == [
        "same", "same"
    ]


def test_db_child_tip_reconciles_after_spool_commit_write_failure(tmp_path, monkeypatch):
    spool = DurableCompressionTurnSpool(tmp_path / "spool")
    spool.enqueue("parent", _turn("queued"), token_count=1, client_turn_id="turn")
    epoch = spool.begin_attempt("parent", "owner")
    original_write = spool._write_json
    failed = False

    def fail_state_once(path, payload, **kwargs):
        nonlocal failed
        if not failed and Path(path).name.startswith("state-"):
            failed = True
            raise OSError("state commit failed")
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(spool, "_write_json", fail_state_once)
    with __import__("pytest").raises(OSError, match="state commit failed"):
        spool.commit_attempt("parent", "owner", epoch, live_tip="child")

    # SessionDB has already published child. The same fenced owner can repair
    # the spool boundary and the pending turn drains only to that child.
    assert spool.reconcile_canonical_tip(
        "parent", "child", expected_owner="owner", expected_epoch=epoch
    )
    seen = []
    assert spool.drain(
        "parent", lambda row, tip: seen.append((row["client_turn_id"], tip))
    ) == ["turn"]
    assert seen == [("turn", "child")]


def test_in_place_tip_reconciles_after_state_commit_failure(tmp_path, monkeypatch):
    spool = DurableCompressionTurnSpool(tmp_path / "spool")
    spool.enqueue("same", _turn("queued"), token_count=1, client_turn_id="turn")
    epoch = spool.begin_attempt("same", "owner")
    original_write = spool._write_json
    failed = False

    def fail_state_once(path, payload, **kwargs):
        nonlocal failed
        if not failed and Path(path).name.startswith("state-"):
            failed = True
            raise OSError("state commit failed")
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(spool, "_write_json", fail_state_once)
    with pytest.raises(OSError, match="state commit failed"):
        spool.commit_attempt("same", "owner", epoch, live_tip="same")
    assert spool.reconcile_canonical_tip(
        "same", "same", expected_owner="owner", expected_epoch=epoch
    )
    state = spool._state("same")
    assert state["owner"] is None
    assert state["committed_epoch"] == epoch
    seen = []
    assert spool.drain("same", lambda row, tip: seen.append((row["client_turn_id"], tip))) == ["turn"]
    assert seen == [("turn", "same")]
