"""Behavioral tests for concurrent compression across distinct and shared sessions.

Complements ``test_compression_concurrent_fork.py`` (which tests the
agent-level lock against a real ``SessionDB``) by focusing on gateway-level
isolation guarantees:

1. Five distinct sessions compressing in parallel must not alias each other's
   session_ids (no cross-session contamination).
2. Two agents sharing the same session_id must serialize: exactly one rotates,
   the other returns its input unchanged (the no-op / lock-loser contract).

The stub-compressor pattern mirrors ``test_compression_concurrent_fork.py``:
the compressor returns deterministic output and sleeps briefly so threads
actually overlap at the OS level, making the absence of aliasing a genuine
stress test rather than a timing accident.
"""

from __future__ import annotations

import os
import multiprocessing
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from agent.conversation_compression import DurableCompressionTurnSpool


@pytest.mark.parametrize("probe, expected", [(False, "dead_or_reused"), (None, "unknown"), (PermissionError(), "unknown")])
def test_compression_liveness_never_signals_process(monkeypatch, probe, expected):
    import psutil
    def pid_exists(_pid):
        if isinstance(probe, Exception):
            raise probe
        return probe
    monkeypatch.setattr(psutil, "pid_exists", pid_exists)
    def forbidden_signal(*_args):
        raise AssertionError("liveness must not send a signal")
    monkeypatch.setattr(os, "kill", forbidden_signal)
    assert DurableCompressionTurnSpool._process_liveness(os.getpid()) == (expected, None)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_agent_with_db(db: SessionDB, session_id: str):
    """Construct an AIAgent wired to *db* and pinned to *session_id*.

    Mirrors the helper in test_compression_concurrent_fork.py exactly so the
    two test modules can be read side-by-side without cognitive overhead.
    """
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    # Stub the compressor: deterministic output, brief sleep to force thread overlap.
    compressor = MagicMock()

    def _compress_with_overlap(*_a, **_kw):
        time.sleep(0.2)  # match fork test sleep so threads reliably overlap
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

    compressor.compress.side_effect = _compress_with_overlap
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    # ROTATION fallback path — pin in_place=False so these keep covering the
    # concurrent-rotation lock contract regardless of the global default
    # (flipped to True in #38763).
    agent.compression_in_place = False
    return agent


_MESSAGES = [{"role": "user", "content": f"m{i}"} for i in range(20)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_concurrent_compressions_same_session_serialize(tmp_path: Path) -> None:
    """Two agents sharing a session_id must not both rotate it.

    The per-session compression lock (added in #34351) serializes concurrent
    compress() calls keyed on the same session_id.  Exactly one agent must
    rotate (the lock winner); the other must return its messages unchanged (the
    lock loser, which detects ``len(returned) == len(input)`` and backs off).

    This is the gateway analogue of the fork test in
    ``test_compression_concurrent_fork.py`` but scoped to the two-agent /
    same-session shape most likely to occur in practice: the main-turn agent
    and its background-review fork both hitting the compression threshold.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    shared_sid = "SHARED_SESSION_CONCURRENT"
    db.create_session(shared_sid, source="discord")

    agent_a = _build_agent_with_db(db, shared_sid)
    agent_b = _build_agent_with_db(db, shared_sid)

    # Force genuine simultaneous lock contention instead of relying on a
    # ``time.sleep`` inside the compressor stub to make the threads overlap.
    # Under CI CPU starvation that sleep is not enough: one thread could
    # acquire → compress → rotate → RELEASE the lock before the other even
    # reaches ``try_acquire``, so both would acquire on the shared id and
    # both would compress (the historical "got 2" flake). A two-party
    # barrier in front of the real acquire guarantees both threads are
    # contending for the lock at the same instant, which is exactly the
    # condition this test means to assert — with zero timing dependency.
    barrier = threading.Barrier(2, timeout=15)
    _real_acquire = db.try_acquire_compression_lock

    def _barriered_acquire(*args, **kwargs):
        # Rendezvous both callers, then let the real (atomic) acquire decide
        # the single winner. Tolerate a broken barrier so a test-side timeout
        # never masquerades as a lock-logic failure.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return _real_acquire(*args, **kwargs)

    db.try_acquire_compression_lock = _barriered_acquire

    results: dict[str, list | None] = {"a": None, "b": None}
    errors: list[Exception] = []

    def run(key, agent):
        try:
            compressed, _sp = agent._compress_context(_MESSAGES, "sys", approx_tokens=120_000)
            results[key] = compressed
        except Exception as exc:
            errors.append(exc)

    t_a = threading.Thread(target=run, args=("a", agent_a), name="main_turn")
    t_b = threading.Thread(target=run, args=("b", agent_b), name="review_fork")
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)

    # Restore the real method so the post-join lock-leak assertion below
    # (and any future call) hits the unwrapped implementation.
    db.try_acquire_compression_lock = _real_acquire

    assert not errors, f"Compression raised exceptions: {errors}"

    # Count which agents actually compressed (returned fewer messages than input)
    compressed_count = sum(
        1 for msgs in results.values()
        if msgs is not None and len(msgs) < len(_MESSAGES)
    )
    unchanged_count = sum(
        1 for msgs in results.values()
        if msgs is not None and len(msgs) == len(_MESSAGES)
    )

    assert compressed_count == 1, (
        f"Expected exactly one agent to compress, got {compressed_count}. "
        "If both compressed, the lock failed to serialize. "
        "If neither compressed, both lost the lock (check lock logic)."
    )
    assert unchanged_count == 1, (
        f"Expected exactly one agent to return messages unchanged (lock loser), "
        f"got {unchanged_count}."
    )

    # Exactly one session_id rotation must have occurred.
    rotated = sum(
        1 for a in (agent_a, agent_b) if a.session_id != shared_sid
    )
    assert rotated == 1, (
        f"Expected exactly one agent to rotate session_id, got {rotated}. "
        "Both agents rotating produces a session fork (Damien's incident shape)."
    )

    # The lock must be released so future compression on the NEW session_id works.
    assert db.get_compression_lock_holder(shared_sid) is None, (
        "Compression lock leaked: still held on the parent session_id after both "
        "threads joined. Future compression on the child session would deadlock."
    )


def test_compression_attempt_epoch_refuses_stale_owner_commit(tmp_path: Path) -> None:
    spool = DurableCompressionTurnSpool(tmp_path / "compression-spool")
    epoch = spool.begin_attempt("shared", "winner")
    assert epoch == 1
    assert spool.begin_attempt("shared", "loser") is None
    assert spool.commit_attempt("shared", "loser", epoch, live_tip="bad") is False
    assert spool.commit_attempt("shared", "winner", epoch, live_tip="child") is True
    assert spool.commit_attempt("shared", "winner", epoch, live_tip="stale") is False
    assert spool.resolve_live_tip("shared") == "child"


def _hold_spool_owner(root: str, ready, release) -> None:
    spool = DurableCompressionTurnSpool(root)
    epoch = spool.begin_attempt("shared", "child-owner", lease_seconds=0.1)
    ready.put(epoch)
    release.get(timeout=10)


def _run_spool_fence_operation(root: str, start, results, operation: str) -> None:
    spool = DurableCompressionTurnSpool(root)
    start.wait(timeout=10)
    if operation == "enqueue":
        result = spool.enqueue(
            "root", {"role": "user", "content": "multiprocess"},
            token_count=1, client_turn_id="mp-turn",
        )["durable"]
    elif operation == "commit":
        epoch = spool.begin_attempt("root", "commit-owner")
        result = bool(
            epoch
            and spool.commit_attempt(
                "root", "commit-owner", epoch, live_tip="child"
            )
        )
    elif operation == "abort":
        epoch = spool.begin_attempt("abort-session", "abort-owner")
        result = bool(
            epoch
            and spool.abort_attempt("abort-session", "abort-owner", epoch)
        )
    elif operation == "reconcile":
        epoch = spool.begin_attempt("reconcile-session", "reconcile-owner")
        result = bool(
            epoch
            and spool.reconcile_canonical_tip(
                "reconcile-session",
                "reconcile-tip",
                expected_owner="reconcile-owner",
                expected_epoch=epoch,
            )
        )
    else:  # pragma: no cover - test helper guard
        raise AssertionError(operation)
    results.put((operation, result))


def _drain_spool_in_process(root: str, results) -> None:
    spool = DurableCompressionTurnSpool(root)
    seen = []
    drained = spool.drain(
        "root", lambda row, tip: seen.append((row["client_turn_id"], tip))
    )
    results.put((drained, seen))


def test_live_process_owner_cannot_be_taken_over(tmp_path: Path) -> None:
    root = str(tmp_path / "compression-spool")
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    release = ctx.Queue()
    process = ctx.Process(target=_hold_spool_owner, args=(root, ready, release))
    process.start()
    assert ready.get(timeout=10) == 1
    time.sleep(0.2)  # durable lease expired, but owner process is still alive
    spool = DurableCompressionTurnSpool(root)
    assert spool.begin_attempt(
        "shared", "contender", allow_stale_owner_takeover=True
    ) is None
    release.put(True)
    process.join(timeout=10)
    assert process.exitcode == 0
    epoch = spool.begin_attempt(
        "shared", "contender", allow_stale_owner_takeover=True
    )
    assert epoch == 2
    assert spool.commit_attempt("shared", "child-owner", 1, live_tip="bad") is False
    assert spool.commit_attempt("shared", "contender", 2, live_tip="good") is True
    assert spool.resolve_live_tip("shared") == "good"


def test_multiprocess_lineage_fence_operations_terminate_canonically(
    tmp_path: Path,
) -> None:
    root = str(tmp_path / "compression-spool")
    # Establish the shared root and global lock before the contention phase;
    # this test targets lock ordering, not first-create path races (covered by
    # the dedicated canonical-root adversarial tests).
    DurableCompressionTurnSpool(root).records("seed")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    operations = ("enqueue", "commit", "abort", "reconcile")
    processes = [
        ctx.Process(
            target=_run_spool_fence_operation,
            args=(root, start, results, operation),
        )
        for operation in operations
    ]
    for process in processes:
        process.start()
    start.set()
    observed = dict(results.get(timeout=15) for _ in operations)
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert observed == {operation: True for operation in operations}

    drain_results = ctx.Queue()
    drainer = ctx.Process(target=_drain_spool_in_process, args=(root, drain_results))
    drainer.start()
    drained, seen = drain_results.get(timeout=15)
    drainer.join(timeout=15)
    assert drainer.exitcode == 0
    assert drained == ["mp-turn"]
    assert seen == [("mp-turn", "child")]
    spool = DurableCompressionTurnSpool(root)
    assert spool.resolve_live_tip("root") == "child"
    assert spool.pending("root") == []


def test_expired_owner_cannot_publish_live_tip(tmp_path: Path) -> None:
    spool = DurableCompressionTurnSpool(tmp_path / "compression-spool")
    epoch = spool.begin_attempt("shared", "owner", lease_seconds=0.01)
    time.sleep(0.02)
    assert spool.commit_attempt("shared", "owner", epoch, live_tip="bad") is False
    assert spool.resolve_live_tip("shared") == "shared"


def test_pid_reuse_and_wall_clock_rollback_do_not_wedge_takeover(
    tmp_path: Path, monkeypatch
) -> None:
    spool = DurableCompressionTurnSpool(tmp_path / "compression-spool")
    monkeypatch.setattr(
        spool, "_process_liveness", lambda _pid: ("alive", "boot-a:proc-old")
    )
    assert spool.begin_attempt("shared", "old", lease_seconds=0.01) == 1
    monkeypatch.setattr(time, "time_ns", lambda: 1)
    monkeypatch.setattr(
        spool, "_process_liveness", lambda _pid: ("alive", "boot-a:proc-reused")
    )
    assert spool.begin_attempt(
        "shared", "new", allow_stale_owner_takeover=True
    ) == 2
    assert spool.commit_attempt("shared", "old", 1, live_tip="stale") is False
    assert spool.commit_attempt("shared", "new", 2, live_tip="child") is True


def test_live_owner_identity_is_never_stolen_after_wall_clock_jump(
    tmp_path: Path, monkeypatch
) -> None:
    spool = DurableCompressionTurnSpool(tmp_path / "compression-spool")
    monkeypatch.setattr(
        spool, "_process_liveness", lambda _pid: ("alive", "boot-a:live")
    )
    assert spool.begin_attempt("shared", "live", lease_seconds=0.01) == 1
    monkeypatch.setattr(time, "time_ns", lambda: 10**30)
    assert spool.begin_attempt(
        "shared", "contender", allow_stale_owner_takeover=True
    ) is None


def test_unknown_process_identity_is_fail_closed_for_takeover(
    tmp_path: Path, monkeypatch
) -> None:
    spool = DurableCompressionTurnSpool(tmp_path / "compression-spool")
    monkeypatch.setattr(
        spool, "_process_liveness", lambda _pid: ("alive", "known-owner")
    )
    assert spool.begin_attempt("shared", "owner") == 1
    monkeypatch.setattr(
        spool, "_process_liveness", lambda _pid: ("unknown", None)
    )
    assert spool.begin_attempt(
        "shared", "contender", allow_stale_owner_takeover=True
    ) is None


def test_typed_turn_metadata_reaches_pre_persistence_context(monkeypatch) -> None:
    from agent import conversation_loop

    agent = SimpleNamespace(_pending_cli_user_message=None)
    captured = {}

    def capture_build(*args, **kwargs):
        captured.update(kwargs["persist_user_display_metadata"])
        raise RuntimeError("captured")

    monkeypatch.setattr(conversation_loop, "build_turn_context", capture_build)
    with pytest.raises(RuntimeError, match="captured"):
        conversation_loop.run_conversation(
            agent,
            "continue",
            persist_user_display_kind="auto_continue",
            persist_user_display_metadata={"task_count": 2},
        )
    assert captured["task_count"] == 2
    assert captured["_compression_turn_id"].startswith("compression-turn:")


@pytest.mark.parametrize("in_place", [True, False])
def test_unknown_self_identity_defers_then_recovers_and_drains_once(
    tmp_path: Path, monkeypatch, in_place: bool
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = f"unknown-self-{in_place}"
    db.create_session(session_id, source="test")
    agent = _build_agent_with_db(db, session_id)
    agent.compression_in_place = in_place
    spool = DurableCompressionTurnSpool(tmp_path / "compression_turn_spool")
    spool.enqueue(
        session_id,
        {"role": "user", "content": "accepted while owner unavailable"},
        token_count=1_000_001,
        client_turn_id="accepted-turn",
    )
    original_liveness = DurableCompressionTurnSpool._process_liveness
    monkeypatch.setattr(
        DurableCompressionTurnSpool,
        "_process_liveness",
        staticmethod(lambda _pid: ("unknown", None)),
    )

    deferred, _ = agent._compress_context(
        list(_MESSAGES), "sys", approx_tokens=1_000_001
    )
    assert deferred == _MESSAGES
    agent.context_compressor.compress.assert_not_called()
    assert [row["client_turn_id"] for row in spool.pending(session_id)] == [
        "accepted-turn"
    ]
    assert db.get_compression_lock_holder(session_id) is None

    monkeypatch.setattr(
        DurableCompressionTurnSpool,
        "_process_liveness",
        staticmethod(original_liveness),
    )
    recovered, _ = agent._compress_context(
        list(_MESSAGES), "sys", approx_tokens=1_000_001
    )
    assert len(recovered) < len(_MESSAGES)
    assert spool.pending(session_id) == []
    assert [row["status"] for row in spool.records(session_id)] == ["drained"]
    assert db.get_compression_lock_holder(session_id) is None
