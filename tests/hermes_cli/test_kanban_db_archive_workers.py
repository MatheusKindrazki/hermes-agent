"""Focused archive workers behavior contracts."""

from __future__ import annotations

import json

import os

import sqlite3

import subprocess

import sys

import time

import types

from pathlib import Path

import pytest

import hermes_state_wal

from hermes_cli import kanban_db as kb

from hermes_cli import kanban_db_connect as kbc

from hermes_cli import kanban_db_dispatch as kbd

from hermes_cli import kanban_db_workspace as kbw



def test_archive_running_task_terminates_worker(kanban_home, monkeypatch):
    """``archive_task`` on a *running* task must actually signal its host-local
    worker process, not just null ``worker_pid`` in the DB (#76196: a worker
    kept running past its own archive and could still push/complete work
    against a task nothing tracks anymore). The termination outcome is
    auditable via the ``archive_worker_termination`` event."""

    with kbc.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        # A verified spawn: an uncaptured fingerprint would (correctly) refuse the signal.
        monkeypatch.setattr(kbd, "_process_fingerprint", lambda _pid: "boot:1|777")
        kbd._set_worker_pid(conn, t, 54321)

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        signalled = []
        assert kb.archive_task(
            conn, t, signal_fn=lambda pid, sig: signalled.append((pid, sig)),
        ) is True

        assert signalled and signalled[0][0] == 54321

        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'archive_worker_termination'",
            (t,),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["prev_pid"] == 54321
        assert payload["host_local"] is True
        assert payload["termination_attempted"] is True
        assert payload["terminated"] is True
        assert kb.get_task(conn, t).status == "archived"


def test_archive_non_running_task_does_not_attempt_termination(kanban_home):
    """A never-claimed (``triage``/``ready``/``done``) task has no live worker:
    ``archive_task`` must not signal anything, and no termination event is
    recorded — only for tasks that were actually ``running`` at archive time."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        signalled = []
        assert kb.archive_task(
            conn, t, signal_fn=lambda pid, sig: signalled.append((pid, sig)),
        ) is True
        assert signalled == []
        row = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND kind = 'archive_worker_termination'",
            (t,),
        ).fetchone()
        assert row is None


# Shared fixtures are registered through pytest, not imported as unused names.
pytest_plugins = ('tests.hermes_cli.test_kanban_db',)
