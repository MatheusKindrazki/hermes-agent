"""The CLI spells out auto-resume when a delegate_task goes to the background.

A top-level ``delegate_task`` returns a handle immediately and runs the subagent
in the background; the result re-enters the conversation as a fresh turn when it
finishes. ``_on_tool_complete`` prints a one-line, no-spinner reassurance at
dispatch so the idle prompt doesn't read as "nothing happened".

The notice is keyed on the tool's own payload (``status`` + ``mode``), so what
keeps it honest under durable admission (K8) is that ``delegate_task`` only ever
emits that payload from behind the Work gate. A refused admission returns a
different, typed shape — and a user who is told "I'll resume when it finishes"
about work that never started has been told something false, which is why the
refusal shapes are pinned here rather than only at the tool boundary.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cli
from cli import HermesCLI


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._pending_edit_snapshots = {}
    return cli_obj


def _capture(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(cli, "_cprint", lambda text: printed.append(text))
    return printed


def test_background_dispatch_prints_resume_notice(monkeypatch):
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({"status": "dispatched", "mode": "background", "count": 1})
    cli_obj._on_tool_complete("tc1", "delegate_task", {"goal": "x"}, result)

    joined = "\n".join(printed)
    assert "resume" in joined.lower()
    assert "it finishes" in joined


def test_background_batch_dispatch_pluralizes(monkeypatch):
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({"status": "dispatched", "mode": "background", "count": 3})
    cli_obj._on_tool_complete("tc2", "delegate_task", {"tasks": []}, result)

    joined = "\n".join(printed)
    assert "3 tasks" in joined
    assert "they finish" in joined


def test_synchronous_delegate_result_prints_no_notice(monkeypatch):
    """A non-background result (e.g. the stateless sync fallback) must not claim
    a background dispatch."""
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({"results": [{"status": "completed", "summary": "done"}]})
    cli_obj._on_tool_complete("tc3", "delegate_task", {"goal": "x"}, result)

    assert not any("resume" in p.lower() for p in printed)


def test_non_delegate_tool_prints_no_notice(monkeypatch):
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    cli_obj._on_tool_complete("tc4", "read_file", {"path": "a"}, '{"ok": true}')

    assert not any("resume" in p.lower() for p in printed)


# --------------------------------------------------------------------------- #
# Durable admission (K8): a refusal must never read as running work
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "handoff_required",
            "handoff": {
                "dispatcher": "hermes-work-control",
                "reason": "long_execution_not_local",
                "work_id": None,
                "request_key": "k8-abc",
            },
            "retryable": False,
            "error": "Nothing was started.",
            "count": 1,
        },
        {
            "status": "admission_required",
            "reason": "no_inherited_admission",
            "retryable": False,
            "error": "Nothing was started.",
        },
        {
            "status": "rejected",
            "reason": "session_bound_dispatch_refused",
            "error": "Nothing was started.",
        },
    ],
    ids=["handoff", "no-inherited-admission", "registry-refused"],
)
def test_no_notice_for_work_that_never_started(monkeypatch, payload):
    """Saying "I\'ll resume when it finishes" about work that was never started
    is worse than saying nothing: the user waits for a result that cannot come.

    The notice is keyed on ``status == "dispatched"`` and ``mode ==
    "background"``, and under durable admission ``delegate_task`` emits that
    pair only from behind the Work gate — so every refusal shape must be
    silent here.
    """
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    cli_obj._on_tool_complete("tc5", "delegate_task", {"goal": "x"}, json.dumps(payload))

    joined = "\n".join(printed).lower()
    assert "resume" not in joined
    assert "running" not in joined
    assert "background" not in joined


K7_ROOT = Path(
    "/Users/matheuskindrazki/development/personal/.worktrees/"
    "hermes-personal-os/hermes-kernel-v1-k7-kernel-20260902"
)
K7_BIN = K7_ROOT / "control" / "kernel" / "admit_cli.py"
K7_SCHEMA = K7_ROOT / "control" / "schemas" / "work-envelope.schema.json"

requires_k7 = pytest.mark.skipif(
    not (K7_BIN.is_file() and os.access(K7_BIN, os.X_OK) and K7_SCHEMA.is_file()),
    reason="K7 admitter worktree not present on this host",
)


def _arm(monkeypatch, tmp_path):
    import yaml

    from agent import durable_admission

    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "responses": [
                    {
                        "outcome": "ok",
                        "work_id": "01a061a7-cea0-7503-b308-1f4029d450c8",
                        "authority_version": 1,
                        "work_status": "open",
                        "created": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KERNEL_V1_MODE", "enforce")
    monkeypatch.setenv("HERMES_KERNEL_ADMITTER_BIN", str(K7_BIN))
    monkeypatch.setenv("HERMES_KERNEL_SCHEMA_PATH", str(K7_SCHEMA))
    monkeypatch.setenv("HERMES_KERNEL_SCHEMA_SHA256", durable_admission.SCHEMA_SHA256)
    monkeypatch.setenv("HERMES_KERNEL_TEST_FIXTURE", "1")
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "durable_admission": {
                        "transport_fixture": str(fixture),
                        "state_dir": str(tmp_path / "kernel-state"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    from hermes_cli.config import _LOAD_CONFIG_CACHE, _RAW_CONFIG_CACHE

    _LOAD_CONFIG_CACHE.clear()
    _RAW_CONFIG_CACHE.clear()


@requires_k7
def test_a_real_handed_off_delegation_prints_no_notice(tmp_path, monkeypatch):
    """End to end against the REAL kernel: the tool hands the work off, and the
    line the CLI would print about it claims nothing."""
    import threading

    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(
        delegate_tool,
        "_build_child_agent",
        lambda **kw: (_ for _ in ()).throw(AssertionError("a child was built")),
    )
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "k8-session-alpha"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._session_db = None

    raw = delegate_tool.delegate_task(
        goal="audit the whole repo", background=True, parent_agent=parent
    )

    cli_obj = _make_cli()
    printed = _capture(monkeypatch)
    cli_obj._on_tool_complete("tc7", "delegate_task", {"goal": "x"}, raw)

    assert json.loads(raw)["status"] == "handoff_required"
    assert printed == []
