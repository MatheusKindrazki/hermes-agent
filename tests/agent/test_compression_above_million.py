"""K6 regression: durable turns while >1M-token compression is blocked.

The payloads stay tiny. ``token_count=1_000_001`` is synthetic telemetry, not
an allocation of a million-token transcript.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from agent.conversation_compression import (
    DurableCompressionTurnSpool,
    drain_compression_turns_into_agent,
)
from agent.conversation_loop import _compression_deferred_result


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
