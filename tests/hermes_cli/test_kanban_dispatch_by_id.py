from __future__ import annotations

import os

import pytest

from hermes_cli import kanban_db as kb


def _ready(conn, title: str, *, assignee: str = "worker", tenant=None) -> str:
    return kb.create_task(
        conn,
        title=title,
        assignee=assignee,
        tenant=tenant,
    )


def test_dispatch_by_id_spawns_only_requested_ready_card(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        first = _ready(conn, "first")
        second = _ready(conn, "second")
        third = _ready(conn, "third")
        spawned = []

        result = kb.dispatch_once(
            conn,
            task_id=second,
            spawn_fn=lambda task, workspace: spawned.append(task.id) or 4242,
            reconcile_orphans=False,
        )

        assert spawned == [second]
        assert [row[0] for row in result.spawned] == [second]
        assert kb.get_task(conn, second).status == "running"
        assert kb.get_task(conn, first).status == "ready"
        assert kb.get_task(conn, third).status == "ready"
    finally:
        conn.close()


def test_dispatch_by_id_fails_closed_for_tenant_status_assignee_and_capacity(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        foreign = _ready(conn, "foreign", tenant="tenant-b")
        monkeypatch.setenv("HERMES_TENANT", "tenant-a")
        result = kb.dispatch_once(conn, task_id=foreign, reconcile_orphans=False)
        assert result.target_reason == "tenant_mismatch"
        assert kb.get_task(conn, foreign).status == "ready"

        monkeypatch.delenv("HERMES_TENANT")
        unassigned = _ready(conn, "unassigned", assignee=None)
        result = kb.dispatch_once(conn, task_id=unassigned, reconcile_orphans=False)
        assert result.target_reason == "assignee_required"

        blocked = _ready(conn, "blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (blocked,))
        result = kb.dispatch_once(conn, task_id=blocked, reconcile_orphans=False)
        assert result.target_reason == "status_not_ready"

        capped = _ready(conn, "capped")
        running = _ready(conn, "running")
        assert kb.claim_task(conn, running) is not None
        result = kb.dispatch_once(
            conn,
            task_id=capped,
            max_in_progress_per_profile=1,
            reconcile_orphans=False,
        )
        assert result.target_reason == "assignee_capacity_exhausted"
        assert kb.get_task(conn, capped).status == "ready"
    finally:
        conn.close()


def test_dispatch_by_id_rejects_connection_for_another_board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="one")
    conn = kb.connect(board="one")
    try:
        tid = _ready(conn, "one-card")
        result = kb.dispatch_once(
            conn,
            task_id=tid,
            board="two",
            reconcile_orphans=False,
        )
        assert result.target_reason == "board_mismatch"
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_worker_path_library_exact_hash_and_source_are_required(monkeypatch, tmp_path):
    lib = os.environ["HERMES_WORKER_PATH_LIB"]
    expected = os.environ["K5_WORKER_PATH_SHA256"]
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "claude"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o700)

    env = dict(os.environ)
    env.update(
        HERMES_WORKER_PATH_LIB=lib,
        K5_WORKER_PATH_SHA256=expected,
        HERMES_CLAUDE_SHIM_DIR=str(shim_dir),
        HERMES_CLAUDE_SHIM_BIN=str(shim),
        HERMES_WORK_CONTROL_RESOLVE="0",
    )
    assert kb._validate_worker_path_lib(env) == (lib, expected)

    env["K5_WORKER_PATH_SHA256"] = "0" * 64
    with pytest.raises(RuntimeError, match="^worker_path_hash_mismatch$"):
        kb._validate_worker_path_lib(env)
