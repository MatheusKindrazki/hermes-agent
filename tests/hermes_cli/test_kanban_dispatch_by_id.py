from __future__ import annotations

import os
from pathlib import Path
import subprocess

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


def test_target_identity_is_revalidated_atomically_at_claim(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TENANT", "tenant-a")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        target = _ready(conn, "target", tenant="tenant-a")
        stale = _ready(conn, "unrelated-stale")
        assert kb.claim_task(conn, stale, ttl_seconds=1) is not None
        conn.execute(
            "UPDATE tasks SET claim_expires = 1 WHERE id = ?", (stale,)
        )
        conn.commit()

        original_claim = kb.claim_task
        mutated = False

        def mutate_then_claim(claim_conn, task_id, **kwargs):
            nonlocal mutated
            if not mutated and task_id == target:
                mutated = True
                other = kb.connect()
                try:
                    other.execute(
                        "UPDATE tasks SET tenant = 'tenant-b' WHERE id = ?",
                        (target,),
                    )
                    other.commit()
                finally:
                    other.close()
            return original_claim(claim_conn, task_id, **kwargs)

        monkeypatch.setattr(kb, "claim_task", mutate_then_claim)
        spawned = []
        result = kb.dispatch_once(
            conn,
            task_id=target,
            spawn_fn=lambda task, workspace: spawned.append(task.id) or 7,
            reconcile_orphans=False,
        )

        assert spawned == []
        assert result.target_reason == "tenant_mismatch"
        assert kb.get_task(conn, target).status == "ready"
        assert kb.get_task(conn, target).tenant == "tenant-b"
        # Targeted dispatch must not perform unrelated housekeeping before
        # its atomic target decision.
        assert kb.get_task(conn, stale).status == "running"
    finally:
        conn.close()


@pytest.mark.parametrize("targeted", [False, True])
def test_invalid_worker_path_has_zero_dispatch_effects(
    monkeypatch, tmp_path, targeted
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_WORKER_PATH_LIB", os.environ["HERMES_WORKER_PATH_LIB"]
    )
    monkeypatch.setenv("K5_WORKER_PATH_SHA256", "0" * 64)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _: True)
    retags = []
    popens = []
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", retags.append)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **kw: popens.append((a, kw)) or pytest.fail("Popen called"),
    )
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        target = _ready(conn, "target")
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (target,)
        ).fetchone()[0]
        result = kb.dispatch_once(
            conn,
            task_id=target if targeted else None,
            reconcile_orphans=False,
        )

        task = kb.get_task(conn, target)
        assert result.worker_path_error == "worker_path_hash_mismatch"
        if targeted:
            assert result.target_reason == "worker_path_hash_mismatch"
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.workspace_path is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (target,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (target,)
        ).fetchone()[0] == before_events
        assert retags == []
        assert popens == []
        assert not kb.worker_logs_dir().exists()
    finally:
        conn.close()


def test_worker_path_snapshot_survives_pathname_swap(monkeypatch, tmp_path):
    approved = tmp_path / "worker_path.sh"
    approved.write_text("export SNAPSHOT_MARKER=approved\n", encoding="utf-8")
    expected = __import__("hashlib").sha256(approved.read_bytes()).hexdigest()
    env = dict(os.environ)
    env.update(
        HERMES_WORKER_PATH_LIB=str(approved),
        K5_WORKER_PATH_SHA256=expected,
    )

    snapshot = kb._snapshot_worker_path_lib(env)
    try:
        replacement = tmp_path / "replacement.sh"
        replacement.write_text("exit 99\n", encoding="utf-8")
        replacement.replace(approved)

        probe = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1" && test "$SNAPSHOT_MARKER" = approved',
                "snapshot-test",
                snapshot.source_path,
            ],
            env=env,
            pass_fds=(snapshot.fd,),
            check=False,
        )
        assert probe.returncode == 0
    finally:
        snapshot.close()
