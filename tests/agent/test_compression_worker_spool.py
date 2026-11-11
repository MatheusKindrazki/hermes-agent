"""Deferred compression input persists through the managed worker owner API."""
import json
import pytest
from types import SimpleNamespace

from agent.conversation_compression_spool import DurableCompressionTurnSpool, drain_compression_turns_into_agent
from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution


def test_managed_worker_drains_deferred_input_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s", "cli")
        epoch = begin_runtime_epoch(db, instance_id="fixture")
        scope = dict(epoch=epoch, execution_id="w", session_id="s", generation=0)
        register_worker_execution(db, **scope, kind="compute", adoption_secret="secret")
        store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / "outbox")
        try:
            spool = DurableCompressionTurnSpool(tmp_path / "compression_turn_spool")
            spool.enqueue("s", {"role": "user", "content": "change direction", "platform_message_id": "m1"},
                          token_count=1_000_001, client_turn_id="t1")
            agent = SimpleNamespace(_session_db=store)
            live = []
            assert drain_compression_turns_into_agent(agent, "s", live) == ["t1"]
            assert drain_compression_turns_into_agent(agent, "s", live) == []
            assert [m["content"] for m in db.get_messages("s")] == ["change direction"]
            assert store.has_platform_message_id("s", "m1")
            assert not store.has_platform_message_id("s", "missing")
            with pytest.raises(WorkerPersistenceError):
                store.has_platform_message_id("another-session", "m1")
            assert len(live) == 1 and live[0]["_db_persisted"]
            record_path = next((tmp_path / "compression_turn_spool").glob("turn-*.json"))
            record = json.loads(record_path.read_text())
            record["status"] = "pending"  # crash after DB commit, before spool acknowledgment
            record_path.write_text(json.dumps(record))
            assert drain_compression_turns_into_agent(agent, "s", []) == ["t1"]
            assert [m["content"] for m in db.get_messages("s")] == ["change direction"]
        finally:
            store.close()
    finally:
        db.close()
