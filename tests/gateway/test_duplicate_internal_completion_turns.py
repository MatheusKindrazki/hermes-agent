"""Regression: equivalent internal events for ONE unit of work make ONE turn.

Reported symptom: several internal events belonging to the same piece of work
(a wrapper script and the process it wraps, the same marker line seen by two
producers, a watch match immediately followed by its completion) each enter the
gateway session as a *new synthetic user message*, so the agent answers the same
conclusion three or four times.

Two mitigations already exist and neither covers this:

* Short-window fan-in (``_enqueue_process_completion_notification`` ->
  ``_flush_process_completion_batch``, window 0.1s) and the per-drain grouping in
  ``_async_delegation_watcher`` only merge events that arrive in the SAME tick.
  A wrapper exits seconds after its child, and completions that straddle turn
  boundaries never see each other.
* The delivery ledger keyed on ``_completion_delivery_identity`` — ``(type,
  session_id|delegation_id, started_at)`` — is a *producer* identity. Two
  producers reporting one unit of work have two identities, so both deliver.

The contract pinned here is deliberately narrow:

* equivalent internal events for the same work and the same route collapse to
  one synthetic turn, even across ticks;
* genuinely distinct conclusions keep their own turn — nothing is lost;
* a different route never coalesces with another;
* the durable async-delegation ack semantics are untouched (a delegation is its
  own unit of work; two delegations always deliver and are both acked).

These are behaviour assertions on adapter injections, not source-text checks.
"""

from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


# A realistic wrapper/inner payload: the wrapper tees its child's log, so both
# producers report the SAME substantial text under different ids and commands.
BUILD_LOG = (
    "> hermes-web@1.4.0 build\n"
    "> vite build --mode production\n"
    "vite v5.4.2 building for production...\n"
    "transforming... 412 modules transformed.\n"
    "BUILD SUCCEEDED in 41.3s\n"
)
MIGRATION_LOG = (
    "Applying migration 0042_add_delivery_claim...\n"
    "  -> altered async_delegations (2 columns)\n"
    "MIGRATION DONE in 1.9s\n"
)
TEST_FAILURE_LOG = (
    "FAIL tests/gateway/test_delivery.py::test_ack\n"
    "  AssertionError: expected 'delivered', got 'pending'\n"
    "2 tests failed, 118 passed\n"
)

TASK_ID = "session:agent:main:telegram:dm:123"
ROUTE_KEY = "agent:main:telegram:dm:123:42"
OTHER_ROUTE_KEY = "agent:main:telegram:dm:999:7"


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Keep every durable/registry side effect inside the temp Hermes home."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    return registry


def _runner(monkeypatch, tmp_path, mode: str = "all") -> GatewayRunner:
    (tmp_path / "config.yaml").write_text(
        f"display:\n  background_process_notifications: {mode}\n",
        encoding="utf-8",
    )
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig())
    runner.adapters[Platform.TELEGRAM] = SimpleNamespace(
        send=AsyncMock(), handle_message=AsyncMock(),
    )
    for key, chat_id, thread_id in (
        (ROUTE_KEY, "123", "42"),
        (OTHER_ROUTE_KEY, "999", "7"),
    ):
        runner.session_store._entries[key] = SimpleNamespace(
            origin=SessionSource(
                platform=Platform.TELEGRAM,
                chat_id=chat_id,
                chat_type="dm",
                thread_id=thread_id,
                user_id="owner",
                user_name="alice",
            )
        )
    return runner


def _watch_event(
    session_id: str,
    *,
    output: str = BUILD_LOG,
    pattern: str = "SUCCEEDED",
    command: str = "run.sh",
    session_key: str = ROUTE_KEY,
    task_id: str = TASK_ID,
) -> dict:
    """A ``watch_match`` as ``ProcessRegistry._check_watch_patterns`` emits it."""
    return {
        "type": "watch_match",
        "session_id": session_id,
        "session_key": session_key,
        "task_id": task_id,
        "command": command,
        "pattern": pattern,
        "output": output,
        "suppressed": 0,
        "platform": "telegram",
        "chat_id": "123",
        "user_id": "owner",
        "thread_id": "42",
    }


def _completion_event(
    session_id: str,
    *,
    started_at: float,
    output: str = BUILD_LOG,
    command: str = "npm run build",
    exit_code: int = 0,
    session_key: str = ROUTE_KEY,
    chat_id: str = "123",
    thread_id: str = "42",
    task_id: "str | None" = TASK_ID,
) -> dict:
    """A ``completion`` as the gateway process watcher builds it."""
    event = {
        "type": "completion",
        "session_id": session_id,
        "session_key": session_key,
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": chat_id,
        "thread_id": thread_id,
        "user_id": "owner",
        "started_at": started_at,
        "command": command,
        "exit_code": exit_code,
        "completion_reason": "exited",
        "output": output,
    }
    if task_id is not None:
        event["task_id"] = task_id
    return event


def _turn_texts(adapter) -> list[str]:
    return [call.args[0].text for call in adapter.handle_message.await_args_list]


# ---------------------------------------------------------------------------
# Watch events — the path with no dedupe at all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_equivalent_watch_events_for_one_work_make_one_turn(
    monkeypatch, tmp_path,
):
    """A wrapper and the process it wraps report the same marker once."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    completion_queue = queue.Queue()
    completion_queue.put(_watch_event("proc_wrapper", command="bash deploy-wrapper.sh"))
    completion_queue.put(_watch_event("proc_inner", command="npm run build"))

    await runner._drain_watch_notifications(completion_queue)

    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_equivalent_watch_events_across_drains_make_one_turn(
    monkeypatch, tmp_path,
):
    """Same-tick fan-in is not enough: the wrapper lands a drain later."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    first = queue.Queue()
    first.put(_watch_event("proc_inner", command="npm run build"))
    await runner._drain_watch_notifications(first)

    second = queue.Queue()
    second.put(_watch_event("proc_wrapper", command="bash deploy-wrapper.sh"))
    await runner._drain_watch_notifications(second)

    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_distinct_watch_conclusions_each_keep_their_turn(
    monkeypatch, tmp_path,
):
    """Suppression is equivalence-scoped — different findings are not lost."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    completion_queue = queue.Queue()
    completion_queue.put(_watch_event("proc_a", output=MIGRATION_LOG, pattern="DONE"))
    completion_queue.put(_watch_event("proc_b", output=TEST_FAILURE_LOG, pattern="FAILED"))

    await runner._drain_watch_notifications(completion_queue)

    assert adapter.handle_message.await_count == 2
    texts = _turn_texts(adapter)
    assert any("MIGRATION DONE" in text for text in texts)
    assert any("2 tests failed" in text for text in texts)


@pytest.mark.asyncio
async def test_equivalent_watch_events_on_different_routes_both_deliver(
    monkeypatch, tmp_path,
):
    """Two chats that happen to see the same marker are two conversations."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    completion_queue = queue.Queue()
    completion_queue.put(_watch_event("proc_a"))
    completion_queue.put(_watch_event("proc_b", session_key=OTHER_ROUTE_KEY))

    await runner._drain_watch_notifications(completion_queue)

    assert adapter.handle_message.await_count == 2


# ---------------------------------------------------------------------------
# Process completions — producer identity is not work identity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrapper_and_inner_completions_across_ticks_make_one_turn(
    monkeypatch, tmp_path,
):
    """The 0.1s fan-in window cannot see a wrapper that exits seconds later."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    inner = _completion_event("proc_inner", started_at=100.0, command="npm run build")
    wrapper = _completion_event(
        "proc_wrapper", started_at=100.5, command="bash deploy-wrapper.sh",
    )

    await runner._deliver_completion_notification("[IMPORTANT: inner]", inner)
    await runner._deliver_completion_notification("[IMPORTANT: wrapper]", wrapper)

    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_distinct_completion_conclusions_across_ticks_both_deliver(
    monkeypatch, tmp_path,
):
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    ok = _completion_event("proc_ok", started_at=100.0, output=MIGRATION_LOG)
    bad = _completion_event(
        "proc_bad", started_at=101.0, output=TEST_FAILURE_LOG, exit_code=1,
    )

    await runner._deliver_completion_notification("[IMPORTANT: ok]", ok)
    await runner._deliver_completion_notification("[IMPORTANT: bad]", bad)

    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_silent_completions_are_never_collapsed(monkeypatch, tmp_path):
    """No output is no evidence of sameness — two silent jobs stay two turns."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    first = _completion_event("proc_quiet_a", started_at=100.0, output="")
    second = _completion_event("proc_quiet_b", started_at=101.0, output="")

    await runner._deliver_completion_notification("[IMPORTANT: a]", first)
    await runner._deliver_completion_notification("[IMPORTANT: b]", second)

    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_equivalent_completions_on_different_routes_both_deliver(
    monkeypatch, tmp_path,
):
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    here = _completion_event("proc_a", started_at=100.0)
    there = _completion_event(
        "proc_b",
        started_at=100.0,
        session_key=OTHER_ROUTE_KEY,
        chat_id="999",
        thread_id="7",
    )

    await runner._deliver_completion_notification("[IMPORTANT: here]", here)
    await runner._deliver_completion_notification("[IMPORTANT: there]", there)

    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_equivalence_window_expires_so_a_later_rerun_still_surfaces(
    monkeypatch, tmp_path,
):
    """Suppression is a short window, not a permanent mute."""
    import gateway.run as gateway_run

    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    now = [1_000.0]
    monkeypatch.setattr(gateway_run.time, "monotonic", lambda: now[0])

    first = _completion_event("proc_run_1", started_at=100.0)
    await runner._deliver_completion_notification("[IMPORTANT: run 1]", first)

    now[0] += gateway_run._INTERNAL_EQUIVALENCE_TTL_SECONDS + 1.0

    second = _completion_event("proc_run_2", started_at=200.0)
    await runner._deliver_completion_notification("[IMPORTANT: run 2]", second)

    assert adapter.handle_message.await_count == 2


# ---------------------------------------------------------------------------
# Invariants the fix must not break
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suppressed_event_never_reaches_the_adapter_as_a_user_turn(
    monkeypatch, tmp_path,
):
    """Alternation: suppression drops a turn, it never splices into one."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    completion_queue = queue.Queue()
    completion_queue.put(_watch_event("proc_wrapper"))
    completion_queue.put(_watch_event("proc_inner"))

    await runner._drain_watch_notifications(completion_queue)

    assert adapter.handle_message.await_count == 1
    injected = adapter.handle_message.await_args.args[0]
    # Still an internal event: the busy path must keep queueing it silently
    # rather than interrupting a running turn.
    assert injected.internal is True


def test_two_delegations_with_identical_summaries_both_deliver_and_ack(
    monkeypatch, tmp_path, isolated_registry,
):
    """A delegation is its own unit of work — durable ack semantics unchanged.

    Two distinct ``delegation_id``s are two pieces of work even when their
    summaries read identically. Both must reach the user and both durable rows
    must end up ``delivered`` — never suppressed as "equivalent", which would
    leave a row pending forever or ack work that was never shown.
    """
    import asyncio

    from tools import async_delegation

    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)

    events = []
    for delegation_id in ("deleg_twin_a", "deleg_twin_b"):
        event = {
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "session_key": ROUTE_KEY,
            "goal": "Check the deploy",
            "status": "completed",
            "summary": "Done.",
            "api_calls": 1,
            "duration_seconds": 5.0,
            "dispatched_at": 1000.0,
            "completed_at": 1005.0,
        }
        async_delegation._persist_dispatch({
            "delegation_id": delegation_id,
            "session_key": event["session_key"],
            "origin_ui_session_id": "",
            "parent_session_id": None,
            "dispatched_at": event["dispatched_at"],
        })
        async_delegation._persist_completion(event, {
            "status": "completed", "summary": event["summary"],
        })
        events.append(event)

    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    async def _deliver_both():
        for event in events:
            await runner._deliver_completion_notification(
                f"[ASYNC DELEGATION COMPLETE — {event['delegation_id']}]\nDone.",
                dict(event),
            )

    asyncio.run(_deliver_both())

    assert adapter.handle_message.await_count == 2
    for event in events:
        row = async_delegation.get_durable_delegation(event["delegation_id"])
        assert row is not None
        assert row["delivery_state"] == "delivered"


# ---------------------------------------------------------------------------
# The unit of work is task_id — controls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_payload_in_a_different_task_delivers_twice(
    monkeypatch, tmp_path,
):
    """Equivalence is scoped to ONE unit of work, never to text alone."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    first = _completion_event("proc_a", started_at=100.0, task_id="task-deploy")
    second = _completion_event("proc_b", started_at=101.0, task_id="task-rollback")

    await runner._deliver_completion_notification("[IMPORTANT: a]", first)
    await runner._deliver_completion_notification("[IMPORTANT: b]", second)

    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_legacy_event_without_task_id_fails_open_and_delivers(
    monkeypatch, tmp_path,
):
    """An unstamped event is never suppressed — duplicate turn over lost result."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    first = _completion_event("proc_a", started_at=100.0, task_id=None)
    second = _completion_event("proc_b", started_at=101.0, task_id=None)

    await runner._deliver_completion_notification("[IMPORTANT: a]", first)
    await runner._deliver_completion_notification("[IMPORTANT: b]", second)

    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_gateway_watcher_stamps_task_id_on_its_completion_event(
    monkeypatch, tmp_path,
):
    """The gateway-built completion carries the registry's grouping key.

    Without this the gateway's own copy is the single completion event with no
    unit-of-work key, so cross-producer equivalence could never fire on the
    surface that actually reports process exits.
    """
    import tools.process_registry as pr_module

    runner = _runner(monkeypatch, tmp_path)
    captured: list[dict] = []

    async def _capture(_text, evt):
        captured.append(evt)
        return True

    monkeypatch.setattr(
        runner, "_enqueue_process_completion_notification", _capture,
    )
    session = pr_module.ProcessSession(
        id="proc_watched",
        command="npm run build",
        task_id="task-build-42",
        session_key=ROUTE_KEY,
        started_at=100.0,
        output_buffer=BUILD_LOG,
    )
    session.exited = True
    session.exit_code = 0
    monkeypatch.setattr(
        pr_module.process_registry, "get", lambda _sid: session,
    )
    monkeypatch.setattr(runner, "_load_background_notifications_mode", lambda: "all")

    await runner._run_process_watcher({
        "session_id": "proc_watched",
        "check_interval": 0,
        "session_key": ROUTE_KEY,
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "42",
        "user_id": "owner",
        "notify_on_complete": True,
    })

    assert captured, "the agent-notify branch never built a completion event"
    assert captured[0]["task_id"] == "task-build-42"


@pytest.mark.asyncio
async def test_watch_match_and_the_completion_that_follows_it_collapse(
    monkeypatch, tmp_path,
):
    """One conclusion, two event shapes, one turn.

    ``type`` and ``pattern`` are producer shape, not conclusion: a marker line
    seen by the watch scanner and the same tail reported at exit are the same
    news. The ``watch_match`` reports no exit status, so it cannot conflict.
    """
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    watch_queue = queue.Queue()
    watch_queue.put(_watch_event("proc_build"))
    await runner._drain_watch_notifications(watch_queue)

    await runner._deliver_completion_notification(
        "[IMPORTANT: build finished]",
        _completion_event("proc_build", started_at=100.0),
    )

    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_a_masked_failure_still_gets_its_own_turn(monkeypatch, tmp_path):
    """Same log, different outcome — success and failure are distinct."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    inner = _completion_event("proc_inner", started_at=100.0, exit_code=1)
    wrapper = _completion_event("proc_wrapper", started_at=100.5, exit_code=0)

    await runner._deliver_completion_notification("[IMPORTANT: inner]", inner)
    await runner._deliver_completion_notification("[IMPORTANT: wrapper]", wrapper)

    assert adapter.handle_message.await_count == 2


# ---------------------------------------------------------------------------
# Concurrency — reserve the key, do not merely check it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_equivalent_injections_produce_one_turn(
    monkeypatch, tmp_path,
):
    """The second arrives while the first is still awaiting the adapter.

    A check-await-record sequence lets both pass the check before either
    records, which is exactly the concurrent fan-out this seam exists to
    collapse.
    """
    import asyncio

    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocked(_event):
        entered.set()
        await release.wait()

    adapter.handle_message = AsyncMock(side_effect=_blocked)

    first = asyncio.create_task(runner._deliver_completion_notification(
        "[IMPORTANT: inner]",
        _completion_event("proc_inner", started_at=100.0, command="npm run build"),
    ))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    second = asyncio.create_task(runner._deliver_completion_notification(
        "[IMPORTANT: wrapper]",
        _completion_event("proc_wrapper", started_at=100.5, command="bash wrap.sh"),
    ))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.wait_for(first, timeout=2.0) is True
    assert await asyncio.wait_for(second, timeout=2.0) is None
    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_first_injection_does_not_swallow_the_second(
    monkeypatch, tmp_path,
):
    """A reservation is not a delivery — a waiter behind a failure must retry."""
    import asyncio

    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def _first_fails(_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("adapter blip")

    adapter.handle_message = AsyncMock(side_effect=_first_fails)

    first = asyncio.create_task(runner._deliver_completion_notification(
        "[IMPORTANT: inner]",
        _completion_event("proc_inner", started_at=100.0),
    ))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    second = asyncio.create_task(runner._deliver_completion_notification(
        "[IMPORTANT: wrapper]",
        _completion_event("proc_wrapper", started_at=100.5),
    ))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.wait_for(first, timeout=2.0) is False
    # The conclusion was never delivered, so the second event must not be
    # dropped behind the failed reservation.
    assert await asyncio.wait_for(second, timeout=2.0) is True
    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_a_marker_never_hides_a_failing_exit(monkeypatch, tmp_path):
    """Unknown outcome must not absorb a non-zero exit.

    A ``watch_match`` reports no exit status. If "unknown" counted as
    equivalent to any outcome, a marker line scanned mid-run would suppress the
    completion that says the process then FAILED — the one conclusion that must
    never be swallowed.
    """
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    watch_queue = queue.Queue()
    watch_queue.put(_watch_event("proc_build"))
    await runner._drain_watch_notifications(watch_queue)

    await runner._deliver_completion_notification(
        "[IMPORTANT: build failed]",
        _completion_event("proc_build", started_at=100.0, exit_code=1),
    )

    assert adapter.handle_message.await_count == 2
    assert "build failed" in _turn_texts(adapter)[1]


@pytest.mark.asyncio
async def test_two_identical_failures_still_collapse(monkeypatch, tmp_path):
    """Known outcomes are equivalent when equal — including two failures."""
    runner = _runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]

    inner = _completion_event("proc_inner", started_at=100.0, exit_code=1)
    wrapper = _completion_event(
        "proc_wrapper", started_at=100.5, exit_code=1, command="bash wrap.sh",
    )

    await runner._deliver_completion_notification("[IMPORTANT: inner]", inner)
    await runner._deliver_completion_notification("[IMPORTANT: wrapper]", wrapper)

    assert adapter.handle_message.await_count == 1


def test_outcome_equivalence_truth_table():
    """The compatibility rule itself, stated once and pinned."""
    equivalent = GatewayRunner._outcomes_are_equivalent
    # unknown pairs with success in either direction
    assert equivalent(None, None) is True
    assert equivalent(None, 0) is True
    assert equivalent(0, None) is True
    # unknown NEVER pairs with a failure, in either direction
    assert equivalent(None, 1) is False
    assert equivalent(1, None) is False
    assert equivalent(None, 137) is False
    # known outcomes: equal only
    assert equivalent(0, 0) is True
    assert equivalent(1, 1) is True
    assert equivalent(0, 1) is False
    assert equivalent(1, 2) is False
