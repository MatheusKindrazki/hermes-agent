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
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from agent.conversation_compression import DurableCompressionTurnSpool


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


def test_expired_owner_cannot_publish_live_tip(tmp_path: Path) -> None:
    spool = DurableCompressionTurnSpool(tmp_path / "compression-spool")
    epoch = spool.begin_attempt("shared", "owner", lease_seconds=0.01)
    time.sleep(0.02)
    assert spool.commit_attempt("shared", "owner", epoch, live_tip="bad") is False
    assert spool.resolve_live_tip("shared") == "shared"
