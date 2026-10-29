"""Cancellation hands the spool epoch to the fallback before the old worker unwinds."""
from types import SimpleNamespace

from agent.conversation_compression import _CompactionLifecycle, _CompressionLease
from agent.conversation_compression_spool import compression_turn_spool, register_compression_turn_spool
from hermes_state import SessionDB


def test_cancelled_holder_releases_spool_before_fallback(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("session", source="cli")
    agent = SimpleNamespace(_active_compression_lock_holder="primary")
    def lease(holder):
        result = _CompressionLease(agent, db=db, sid="session", ttl=60,
            refresh_interval=10, commit_fence=None,
            lifecycle=_CompactionLifecycle(agent, False))
        result.holder = holder
        assert db.try_acquire_compression_lock("session", holder, ttl_seconds=60)
        return result
    primary = lease("primary")
    assert register_compression_turn_spool(agent, primary)
    primary.release_holder_only()  # watchdog detaches a still-unwinding worker
    fallback = lease("fallback")
    try:
        assert register_compression_turn_spool(agent, fallback)
        primary.release()  # late unwind cannot abort the successor's epoch
        assert compression_turn_spool().refresh_attempt(
            "session", "fallback", fallback.spool_epoch, lease_seconds=60)
        assert not register_compression_turn_spool(agent, primary)
    finally:
        fallback.release()
        db.close()
