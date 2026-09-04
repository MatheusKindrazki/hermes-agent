from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from tools import kanban_tools


def test_empty_title_rejected_before_cli_connect(monkeypatch):
    @contextlib.contextmanager
    def forbidden_connect(*args, **kwargs):
        raise AssertionError("empty title must be rejected before DB connect")
        yield

    monkeypatch.setattr(kb, "connect_closing", forbidden_connect)
    args = argparse.Namespace(
        title=" \t ",
        workspace="scratch",
        branch=None,
        max_runtime=None,
        max_retries=None,
        body=None,
        assignee="worker",
        created_by="test",
        project=None,
        tenant=None,
        priority=0,
        parent=[],
        triage=False,
        idempotency_key="empty-key",
        skills=[],
        model_override=None,
        provider_override=None,
        goal_mode=False,
        goal_max_turns=None,
        initial_status="running",
        json=False,
    )
    assert kb_cli._cmd_create(args) == 2


def test_empty_title_rejected_before_tool_connect(monkeypatch):
    monkeypatch.setattr(
        kanban_tools,
        "_connect",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("empty title must be rejected before DB connect")
        ),
    )
    output = kanban_tools._handle_create(
        {"title": "\n", "assignee": "worker", "idempotency_key": "empty-key"}
    )
    assert "title is required" in output


def test_empty_title_with_idempotency_leaves_no_task_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="title is required"):
            kb.create_task(conn, title="  ", idempotency_key="empty-key")
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key IS NOT NULL"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_two_connections_same_idempotency_key_return_canonical_id(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    original_write_txn = kb.write_txn
    rendezvous = threading.Barrier(2)

    @contextlib.contextmanager
    def synchronized_write_txn(conn, *, allow_nested=False):
        rendezvous.wait(timeout=5)
        with original_write_txn(conn, allow_nested=allow_nested):
            yield conn

    monkeypatch.setattr(kb, "write_txn", synchronized_write_txn)

    def create(workspace: str) -> str:
        conn = kb.connect()
        try:
            return kb.create_task(
                conn,
                title="same request",
                assignee="worker",
                workspace_kind="dir",
                workspace_path=workspace,
                idempotency_key="canonical-key",
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, [str(tmp_path / "a"), str(tmp_path / "b")]))

    assert ids[0] == ids[1]
    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT id, workspace_path FROM tasks WHERE idempotency_key = ?",
            ("canonical-key",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == ids[0]
    finally:
        conn.close()
