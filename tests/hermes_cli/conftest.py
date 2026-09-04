"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import hashlib
import os

import pytest


_K5_WORKER_PATH_SHA256 = (
    "38810823dfed4a0571498dc26486beff1c8fd4a3d06f7bf9dd0ef224603d8c53"
)


@pytest.fixture(autouse=True)
def _signed_worker_path_env(monkeypatch, tmp_path):
    """Provide a hermetic signed launcher without masking explicit inputs.

    Production always requires HERMES_WORKER_PATH_LIB + its K5 hash. Most
    direct-spawn unit tests predate that boundary, so pytest supplies a tiny
    signed source file only when neither variable was provided by the caller.
    Explicit path/hash values are never rewritten, which keeps negative hash
    and source tests meaningful.

    When the caller selects the real K5 image, provide the prerequisites that
    image validates: an owner-only executable shim and disabled Work-Control
    lookup. This is environment setup only; ``_default_spawn`` remains real.
    """
    explicit_path = os.environ.get("HERMES_WORKER_PATH_LIB")
    explicit_hash = os.environ.get("K5_WORKER_PATH_SHA256")

    if explicit_path is None and explicit_hash is None:
        fallback = tmp_path / "pytest-worker_path.sh"
        fallback.write_text(
            "export HERMES_PYTEST_WORKER_PATH=1\n",
            encoding="utf-8",
        )
        fallback.chmod(0o700)
        digest = hashlib.sha256(fallback.read_bytes()).hexdigest()
        monkeypatch.setenv("HERMES_WORKER_PATH_LIB", str(fallback))
        monkeypatch.setenv("K5_WORKER_PATH_SHA256", digest)

    selected_hash = os.environ.get("K5_WORKER_PATH_SHA256")
    if selected_hash == _K5_WORKER_PATH_SHA256:
        shim_dir = tmp_path / "pytest-claude-shim"
        shim_dir.mkdir(mode=0o700)
        shim = shim_dir / "claude"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o700)
        monkeypatch.setenv("HERMES_CLAUDE_SHIM_DIR", str(shim_dir))
        monkeypatch.setenv("HERMES_CLAUDE_SHIM_BIN", str(shim))
        monkeypatch.setenv("HERMES_WORK_CONTROL_RESOLVE", "0")

    return {
        "explicit_path": explicit_path,
        "explicit_hash": explicit_hash,
    }


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
