"""Focused cooldown rollback behavior contracts."""

from __future__ import annotations

import copy

import json

import os

import sqlite3

import threading

import time

from pathlib import Path

from types import SimpleNamespace

from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


from tests.agent.test_compression_concurrent_fork import (_build_agent_with_db)

@pytest.mark.parametrize("deadline_offset", [-10.0, 0.05, None])
def test_force_cancel_restores_exact_expired_or_expiring_cooldown_row(
    tmp_path: Path,
    deadline_offset: float | None,
) -> None:
    """Cancellation preserves raw cooldown columns even after their deadline."""
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    from agent.context_compressor import ContextCompressor

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = f"RAW_COOLDOWN_{deadline_offset}"
    db.create_session(session_id, source="cli")
    deadline = time.time() + deadline_offset if deadline_offset is not None else None
    db.restore_compression_failure_cooldown_row(
        session_id,
        {
            "session_exists": True,
            "cooldown_until": deadline,
            "error": "expired-but-exact",
        },
    )
    before = db.get_compression_failure_cooldown_row(session_id)

    agent = _build_agent_with_db(db, session_id, stub_compressor=False)
    compressor = agent.context_compressor
    assert isinstance(compressor, ContextCompressor)
    # A stale local persistence-failure marker must not suppress restoration
    # once the raw durable row was captured authoritatively under the lease.
    compressor._cooldown_persist_failed = True
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    real_clear = ContextCompressor._clear_compression_failure_cooldown

    def _mutate_then_cancel() -> None:
        real_clear(compressor)
        if deadline_offset is not None and deadline_offset > 0:
            assert deadline is not None
            while time.time() <= deadline:
                time.sleep(0.005)
        agent._hard_interrupt_requested.set()
        raise AuxiliaryExplicitCancellation()

    compressor._clear_compression_failure_cooldown = _mutate_then_cancel

    compressed, _prompt = agent._compress_context(
        messages,
        "sys",
        approx_tokens=120_000,
        force=True,
    )

    assert compressed is messages
    assert db.get_compression_failure_cooldown_row(session_id) == before
    assert db.get_compression_lock_holder(session_id) is None


def test_exact_cooldown_restore_tolerates_a_concurrently_deleted_session(
    tmp_path: Path,
) -> None:
    """A deleted session has no cooldown state left to compensate (#106271)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "COOLDOWN_ROLLBACK_DELETED_SESSION"
    db.create_session(session_id, source="cli")
    snapshot = db.get_compression_failure_cooldown_row(session_id)
    assert snapshot["session_exists"] is True

    assert db.delete_session(session_id) is True
    db.restore_compression_failure_cooldown_row(session_id, snapshot)

    assert db.get_compression_failure_cooldown_row(session_id) == {
        "session_exists": False,
        "cooldown_until": None,
        "error": None,
    }


def test_exact_cooldown_restore_tolerates_deletion_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deletion after the compensating write is still an intentional no-op (#106271)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "COOLDOWN_ROLLBACK_DELETE_DURING_VERIFY"
    db.create_session(session_id, source="cli")
    snapshot = db.get_compression_failure_cooldown_row(session_id)
    real_execute_write = db._execute_write

    def _restore_then_delete(callback):
        monkeypatch.setattr(db, "_execute_write", real_execute_write)
        result = real_execute_write(callback)
        assert db.delete_session(session_id) is True
        return result

    monkeypatch.setattr(db, "_execute_write", _restore_then_delete)
    db.restore_compression_failure_cooldown_row(session_id, snapshot)

    assert db.get_compression_failure_cooldown_row(session_id)["session_exists"] is False


def test_cooldown_rollback_failure_surfaces_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed compensating write cannot masquerade as a mutation-free cancel."""
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    from agent.context_compressor import ContextCompressor

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "COOLDOWN_ROLLBACK_WRITE_FAILURE"
    db.create_session(session_id, source="cli")
    db.record_compression_failure_cooldown(
        session_id,
        time.time() + 120.0,
        "must-restore",
    )
    agent = _build_agent_with_db(db, session_id, stub_compressor=False)
    compressor = agent.context_compressor
    assert isinstance(compressor, ContextCompressor)
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    real_clear = ContextCompressor._clear_compression_failure_cooldown

    def _mutate_then_cancel() -> None:
        real_clear(compressor)
        agent._hard_interrupt_requested.set()
        raise AuxiliaryExplicitCancellation()

    compressor._clear_compression_failure_cooldown = _mutate_then_cancel

    def _rollback_write_fails(_self, _session_id, _snapshot) -> None:
        raise sqlite3.OperationalError("forced rollback write failure")

    monkeypatch.setattr(
        SessionDB,
        "restore_compression_failure_cooldown_row",
        _rollback_write_fails,
    )

    with pytest.raises(sqlite3.OperationalError, match="forced rollback write failure"):
        agent._compress_context(
            messages,
            "sys",
            approx_tokens=120_000,
            force=True,
        )

    assert db.get_compression_lock_holder(session_id) is None


def test_exact_cooldown_restore_api_propagates_sqlite_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "RAW_COOLDOWN_WRITE_FAILURE"
    db.create_session(session_id, source="test")

    def _write_fails(_callback) -> None:
        raise sqlite3.OperationalError("forced low-level write failure")

    monkeypatch.setattr(db, "_execute_write", _write_fails)

    with pytest.raises(sqlite3.OperationalError, match="forced low-level write failure"):
        db.restore_compression_failure_cooldown_row(
            session_id,
            {
                "session_exists": True,
                "cooldown_until": time.time() + 10.0,
                "error": "must propagate",
            },
        )
