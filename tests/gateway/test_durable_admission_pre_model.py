"""K8 at the gateway ingress: the Signal lands before anything reads the input.

The interactive path enriches the message on the way to the model — clarify and
/update interception, slash commands, the interrupt path (which transcribes
pending voice), session-hygiene compression, then vision enrichment. The A2A
background lane enriches it too, with image descriptions, and reaches
``run_conversation`` without passing the interactive ingress at all.

So the Signal is recorded at ingress, against the PLATFORM's own message
identity, and the verified outcome is carried inward — not re-derived from text
that has since been rewritten.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent import durable_admission

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

OK_RESPONSE = {
    "outcome": "ok",
    "work_id": "01a061a7-cea0-7503-b308-1f4029d450c8",
    "authority_version": 1,
    "work_status": "open",
    "created": True,
}


@pytest.fixture(autouse=True)
def _clean_admission_state():
    durable_admission.reset_state_for_tests()
    yield
    durable_admission.reset_state_for_tests()


def _arm(monkeypatch, tmp_path, responses=(OK_RESPONSE,)):
    import yaml

    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"responses": list(responses)}), encoding="utf-8")
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


def _runner(session_key="agent:main:telegram:4242"):
    """A GatewayRunner shell carrying only what the ingress seam touches.

    ``object.__new__`` is the documented pattern for exercising one method of
    this class without standing up a gateway (see AGENTS.md).
    """
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: session_key
    return runner


def _event(text="ship it", message_id="4242", update_id=None, platform="telegram"):
    event = MagicMock()
    event.text = text
    event.message_id = message_id
    event.platform_update_id = update_id
    source = MagicMock()
    source.platform = MagicMock()
    source.platform.value = platform
    event.source = source
    return event, source


# --------------------------------------------------------------------------- #
# The ingress seam
# --------------------------------------------------------------------------- #


@requires_k7
def test_ingress_records_the_signal_and_lets_the_turn_continue(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    runner = _runner()
    event, source = _event()

    assert runner._k8_admit_inbound(event, source) is None
    assert (tmp_path / "kernel-state" / "inbox.db").is_file(), (
        "the gateway let the message through without recording a Signal"
    )


@requires_k7
def test_ingress_publishes_the_outcome_for_the_turn_to_consume(monkeypatch, tmp_path):
    """The turn seam must not re-admit: by then vision/STT have rewritten the
    text, and hashing that would mint a second Signal for one input."""
    _arm(monkeypatch, tmp_path)
    runner = _runner()
    event, source = _event()

    runner._k8_admit_inbound(event, source)

    carried = durable_admission.consume_pre_admission()
    assert carried is not None
    assert carried.model_may_run is True
    assert carried.content_sha256 == durable_admission.content_sha256("ship it")
    # Taken exactly once, so a later turn cannot ride it.
    assert durable_admission.consume_pre_admission() is None


@requires_k7
def test_identity_is_the_platform_message_not_the_text(monkeypatch, tmp_path):
    """Replaying one platform event is ONE Signal; two same-text messages with
    distinct ids are TWO."""
    _arm(monkeypatch, tmp_path)
    runner = _runner()

    runner._k8_admit_inbound(*_event(text="ok", message_id="1"))
    replay_first = durable_admission.consume_pre_admission()
    runner._k8_admit_inbound(*_event(text="ok", message_id="1"))
    replay_second = durable_admission.consume_pre_admission()
    runner._k8_admit_inbound(*_event(text="ok", message_id="2"))
    distinct = durable_admission.consume_pre_admission()

    # One idempotency key means ONE Signal in the kernel's inbox; the second
    # admission of the same platform event adopts it instead of creating one.
    # (``replayed`` speaks about a WORK, and an inline chat turn opens none.)
    assert replay_first.idempotency_key == replay_second.idempotency_key
    assert replay_first.receipt["signal"]["created"] is True
    assert replay_second.receipt["signal"]["created"] is False
    assert distinct.idempotency_key != replay_first.idempotency_key
    assert distinct.receipt["signal"]["created"] is True


@requires_k7
def test_ingress_admits_under_the_observation_time_sentinel(monkeypatch, tmp_path):
    """The gateway does not stamp a clock into the envelope.

    ``MessageEvent.timestamp`` defaults to ``datetime.now()`` at construction,
    so a redelivery carries a NEW value; using it would fork the Signal. Until a
    per-adapter platform time is plumbed, the truthful value is the sentinel.
    """
    _arm(monkeypatch, tmp_path)
    sent = {}
    real = durable_admission.subprocess.run
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda argv, **kw: sent.update({"input": kw.get("input")}) or real(argv, **kw),
    )

    _runner()._k8_admit_inbound(*_event())

    envelope = json.loads(sent["input"])
    assert envelope["source_event"]["observed_at"] == durable_admission.OBSERVED_AT_UNKNOWN


@requires_k7
def test_ingress_replay_is_byte_identical_across_attempts(monkeypatch, tmp_path):
    """A redelivered platform message produces the identical canonical envelope,
    even though each attempt has its own recording clock."""
    _arm(monkeypatch, tmp_path)
    payloads: list = []
    real = durable_admission.subprocess.run
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda argv, **kw: payloads.append(kw.get("input")) or real(argv, **kw),
    )
    runner = _runner()

    runner._k8_admit_inbound(*_event(text="ok", message_id="4242"))
    runner._k8_admit_inbound(*_event(text="ok", message_id="4242"))

    assert payloads[0] == payloads[1]


@requires_k7
def test_telegram_update_id_is_accepted_when_message_id_is_absent(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    runner = _runner()
    event, source = _event(message_id=None, update_id=99887766)

    assert runner._k8_admit_inbound(event, source) is None
    carried = durable_admission.consume_pre_admission()
    assert carried.event_id.endswith("99887766")


@requires_k7
def test_missing_stable_identity_fails_closed(monkeypatch, tmp_path):
    """Armed with no platform identity, minting one would turn every redelivery
    into a fresh input. The honest answer is to refuse the turn."""
    _arm(monkeypatch, tmp_path)
    runner = _runner()
    event, source = _event(message_id=None, update_id=None)

    reply = runner._k8_admit_inbound(event, source)

    assert reply is not None
    assert "stable message identity" in reply
    assert "running" not in reply.lower()
    assert durable_admission.consume_pre_admission() is None


@requires_k7
def test_a_refused_admission_returns_a_reply_that_never_claims_work_started(
    monkeypatch, tmp_path
):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KERNEL_ADMITTER_BIN", str(tmp_path / "not-there.py"))
    runner = _runner()
    event, source = _event()

    reply = runner._k8_admit_inbound(event, source)

    assert reply is not None
    lowered = reply.lower()
    assert "nothing has run" in lowered
    assert "dispatched" not in lowered and "resume" not in lowered


@requires_k7
def test_two_sessions_do_not_share_a_signal(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)

    _runner("agent:main:telegram:1")._k8_admit_inbound(*_event(text="same", message_id="7"))
    alpha = durable_admission.consume_pre_admission()
    _runner("agent:main:telegram:2")._k8_admit_inbound(*_event(text="same", message_id="7"))
    beta = durable_admission.consume_pre_admission()

    assert alpha.idempotency_key != beta.idempotency_key


def test_unarmed_ingress_is_never_consulted(monkeypatch, tmp_path):
    """Off is off: the seam is guarded by ``admission_enabled`` in the caller,
    and the helper itself spawns nothing when it is not armed."""
    monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)

    def _boom(*a, **kw):
        raise AssertionError("mode off must not spawn the admitter")

    monkeypatch.setattr(durable_admission.subprocess, "run", _boom)

    assert durable_admission.admission_enabled() is False
    assert _runner()._k8_admit_inbound(*_event()) is None


# --------------------------------------------------------------------------- #
# The A2A background lane
# --------------------------------------------------------------------------- #


def _background_runner(tmp_path, monkeypatch, *, adapter_absent=False):
    """A runner whose background lane stops right after the K8 seam.

    ``_run_background_task_inner`` returns early when no adapter resolves, which
    is exactly the boundary we need: reaching that return means the seam let the
    task through, and not reaching it means the seam stopped it first.
    """
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: "agent:main:telegram:4242"
    reached = []

    def _adapter_for_source(source):
        reached.append(source)
        return None

    runner._adapter_for_source = _adapter_for_source
    return runner, reached


@requires_k7
def test_background_lane_admits_before_vision_enrichment(monkeypatch, tmp_path):
    """The A2A lane enriches the prompt with image descriptions before it runs.
    The Signal must precede that, so it is recorded against the task id."""
    _arm(monkeypatch, tmp_path)
    runner, reached = _background_runner(tmp_path, monkeypatch)
    published: list = []
    monkeypatch.setattr(
        durable_admission,
        "publish_pre_admission",
        lambda outcome: published.append(outcome),
    )
    source = MagicMock()

    asyncio.run(
        runner._run_background_task_inner(
            prompt="describe the attachment",
            source=source,
            task_id="task-abc-123",
            media_urls=["/tmp/pic.png"],
            media_types=["image/png"],
        )
    )

    # The adapter lookup sits AFTER the seam, so reaching it proves the seam ran
    # and allowed the task.
    assert reached, "the background lane never reached its adapter resolution"
    assert (tmp_path / "kernel-state" / "inbox.db").is_file()
    # ``asyncio.run`` executes the coroutine in a COPY of this context, so a
    # ContextVar written inside it cannot be read back out here. Recording the
    # publish is how the carry is observed without pretending otherwise.
    assert published, "the background lane recorded a Signal but published nothing"
    assert published[-1].event_id.endswith("task-abc-123")
    assert published[-1].model_may_run is True


@requires_k7
def test_background_lane_stops_when_admission_is_refused(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KERNEL_ADMITTER_BIN", str(tmp_path / "not-there.py"))
    runner, reached = _background_runner(tmp_path, monkeypatch)

    asyncio.run(
        runner._run_background_task_inner(
            prompt="describe the attachment",
            source=MagicMock(),
            task_id="task-abc-123",
            media_urls=["/tmp/pic.png"],
            media_types=["image/png"],
        )
    )

    assert reached == [], "the background task ran past a refused admission"


def test_background_lane_unarmed_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)

    def _boom(*a, **kw):
        raise AssertionError("mode off must not spawn the admitter")

    monkeypatch.setattr(durable_admission.subprocess, "run", _boom)
    runner, reached = _background_runner(tmp_path, monkeypatch)

    asyncio.run(
        runner._run_background_task_inner(
            prompt="anything", source=MagicMock(), task_id="task-abc-123"
        )
    )

    assert reached, "the unarmed background lane did not run as before"


# --------------------------------------------------------------------------- #
# The executor boundary — a ContextVar cleared in a copy cannot un-publish
# --------------------------------------------------------------------------- #


def _outcome(tag: str):
    return durable_admission.AdmissionOutcome(
        state="no_work_required",
        reason_code="inline_answer_no_execution",
        seam="none",
        idempotency_key=tag,
        content_sha256=durable_admission.content_sha256(tag),
        request_key="k8-%s" % tag,
        event_id="evt.probe.%s" % tag,
        session_id="sess-%s" % tag,
        model_may_run=True,
        signal_persisted=True,
    )


def test_red_carrier_survives_the_executor_boundary(monkeypatch):
    """The carrier must be ONE-SHOT across the real executor boundary.

    ``_run_in_executor_with_context`` runs the worker under ``copy_context()``.
    A ContextVar cleared inside that copy does NOT clear the async parent's, so
    a second executor call in the SAME request re-reads the same outcome and a
    second model invocation rides an admission that was already spent.

    RED on unfixed code: the second call sees the outcome again.
    """
    runner = _runner()
    published = _outcome("A")

    async def _probe():
        durable_admission.publish_pre_admission(published)
        first = await runner._run_in_executor_with_context(
            durable_admission.consume_pre_admission
        )
        second = await runner._run_in_executor_with_context(
            durable_admission.consume_pre_admission
        )
        return first, second

    first, second = asyncio.run(_probe())

    assert first is not None and first.idempotency_key == "A"
    assert second is None, (
        "the pre-admission survived the executor boundary and was consumed "
        "twice in one request"
    )


def test_concurrent_requests_never_cross_and_consume_exactly_once():
    """Two concurrent requests, several executor hops each.

    Each task must see ITS own admission exactly once and never the sibling's.
    ``asyncio.Task`` copies the context at creation, so each task owns its own
    carrier; the box makes the take one-shot within a task.
    """
    runner = _runner()

    async def _request(tag: str, hops: int):
        durable_admission.publish_pre_admission(_outcome(tag))
        seen = []
        for _ in range(hops):
            seen.append(
                await runner._run_in_executor_with_context(
                    durable_admission.consume_pre_admission
                )
            )
        return seen

    async def _both():
        return await asyncio.gather(
            asyncio.create_task(_request("A", 3)),
            asyncio.create_task(_request("B", 3)),
        )

    seen_a, seen_b = asyncio.run(_both())

    keys_a = [o.idempotency_key for o in seen_a if o is not None]
    keys_b = [o.idempotency_key for o in seen_b if o is not None]
    assert keys_a == ["A"], "request A did not consume its admission exactly once"
    assert keys_b == ["B"], "request B did not consume its admission exactly once"
    assert seen_a[1:] == [None, None] and seen_b[1:] == [None, None]


def test_parent_invalidation_retires_a_carrier_the_worker_never_took():
    """Every parent-owned exit — refusal, exception, cancellation, proxy,
    background, retry — retires the carrier, so nothing can ride it later."""
    carrier = durable_admission.publish_pre_admission(_outcome("A"))
    assert carrier.peek() is not None

    durable_admission.clear_pre_admission()

    assert carrier.spent is True
    assert carrier.take() is None
    assert durable_admission.consume_pre_admission() is None


def test_a_taken_carrier_cannot_be_taken_again_from_any_thread():
    carrier = durable_admission.publish_pre_admission(_outcome("A"))
    results: list = []
    barrier = threading.Barrier(4)

    def _take():
        barrier.wait(timeout=5)
        results.append(carrier.take())

    threads = [threading.Thread(target=_take) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len([r for r in results if r is not None]) == 1


# --------------------------------------------------------------------------- #
# Ingress -> preprocessing -> executor -> run_conversation, end to end
# --------------------------------------------------------------------------- #


@requires_k7
def test_carrier_survives_preprocessing_and_is_spent_by_the_matching_turn(
    monkeypatch, tmp_path
):
    """The whole journey, with the real pieces at every hop.

    Ingress admits the RAW text and publishes a one-shot carrier; preprocessing
    rewrites the text (vision/STT); the parent picks the carrier up and puts it
    on a real ``TurnContext``; the turn crosses the REAL executor; the worker
    installs it on the agent and the real ``run_conversation`` takes it. The
    enriched text is never admitted, and a second model invocation in the same
    request finds nothing left to ride.
    """
    import types

    from agent import conversation_loop
    from gateway.turn_context import TurnContext

    _arm(monkeypatch, tmp_path)
    runner = _runner()

    admissions: list = []
    real_run = durable_admission.subprocess.run
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda argv, **kw: admissions.append(json.loads(kw["input"])) or real_run(argv, **kw),
    )
    reached: list = []
    monkeypatch.setattr(
        conversation_loop,
        "build_turn_context",
        lambda agent, user_message, *a, **kw: reached.append(user_message)
        or (_ for _ in ()).throw(RuntimeError("turn reached the model path")),
    )

    raw = "what is in this picture"
    enriched = raw + "\n\n[Image: a red bicycle]"
    agent = types.SimpleNamespace(
        session_id="agent:main:telegram:4242",
        platform="telegram",
        api_mode="chat_completions",
        _delegate_depth=0,
    )

    def _turn(ctx: TurnContext, text: str):
        """What _run_sync_inner does: install, call, detach + retire."""
        carrier = getattr(ctx, "k8_pre_admission", None)
        if carrier is not None:
            agent._k8_pre_admission = carrier
        try:
            try:
                conversation_loop.run_conversation(agent, text)
            except RuntimeError:
                pass
            return "allowed"
        finally:
            if carrier is not None:
                carrier.invalidate()
                agent._k8_pre_admission = None

    async def _request():
        # 1. Ingress admits the RAW text, before any enrichment.
        assert runner._k8_admit_inbound(*_event(text=raw, message_id="4242")) is None
        # 2. Parent picks the carrier up in its OWN context (as _run_agent_inner does).
        ctx = TurnContext(k8_pre_admission=durable_admission.current_pre_admission())
        # 3. First model invocation, across the real executor, on ENRICHED text.
        first = await runner._run_in_executor_with_context(_turn, ctx, enriched)
        # 4. Second invocation in the SAME request, no fresh carrier.
        second_carrier = getattr(agent, "_k8_pre_admission", None)
        second_take = durable_admission.consume_pre_admission(ctx.k8_pre_admission)
        return first, second_carrier, second_take

    first, second_carrier, second_take = asyncio.run(_request())

    assert first == "allowed"
    assert reached == [enriched], "the turn did not reach the model path once"
    # Exactly one admission, and it hashed the RAW text — never the enriched one.
    assert len(admissions) == 1
    assert admissions[0]["content_sha256"] == durable_admission.content_sha256(raw)
    assert admissions[0]["content_sha256"] != durable_admission.content_sha256(enriched)
    # Nothing is left for a second model invocation to ride.
    assert second_carrier is None
    assert second_take is None
