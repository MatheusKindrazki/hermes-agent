"""A deadline during BEGIN is a retryable browse timeout, not store corruption."""
from contextlib import closing
import sqlite3

import pytest

import hermes_state_reconcile as reconcile
from hermes_state import SessionDB


def test_browse_deadline_during_begin_preserves_the_store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("kept", source="cli")
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()

    class ExpiringAtBegin(reconcile._Budget):
        def arm(self, conn):
            self.deadline = self._clock() - 1
            super().arm(conn)

    monkeypatch.setattr(reconcile, "_BUDGET_PROGRESS_OPS", 1)
    with pytest.raises(reconcile.BrowseBudgetExceeded):
        reconcile._apply_additive_schema(path, ExpiringAtBegin(30))

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == before
        assert conn.execute("SELECT id FROM sessions").fetchall() == [("kept",)]
