"""K8: long work is handed off, not promised by a registry that dies with us.

The async delegation registry is SESSION-BOUND — its records, its executor and
its recovery all belong to the owning process. That is why a killed owner used
to recover as ``unknown`` naming nothing: the promise outlived nothing. So under
durable admission the answer is not "dispatch it with a receipt", it is "not
here": ``delegate_task`` returns a typed handoff naming the external Work
dispatcher, and the registry's own public APIs refuse to accept the work at all.

What may still run locally is a read-only subtask fully consumed by the current
admitted turn — under the turn's inherited admission, never under a second Work.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent import durable_admission
from tools import async_delegation

K7_ROOT = Path(os.environ.get("HERMES_TEST_K7_ROOT",
    "/Users/matheuskindrazki/development/personal/.worktrees/"
    "hermes-personal-os/hermes-kernel-v1-k7-kernel-20260902"
))
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
def _clean():
    async_delegation._reset_for_tests()
    durable_admission.reset_state_for_tests()
    yield
    async_delegation._reset_for_tests()
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
                },
                # Headroom so the nested-orchestrator case reaches the K8 gate
                # instead of stopping at the unrelated depth limit.
                "delegation": {"max_spawn_depth": 3},
            }
        ),
        encoding="utf-8",
    )
    from hermes_cli.config import _LOAD_CONFIG_CACHE, _RAW_CONFIG_CACHE

    _LOAD_CONFIG_CACHE.clear()
    _RAW_CONFIG_CACHE.clear()


def _rows() -> set:
    with async_delegation._DB_LOCK, async_delegation._transaction() as conn:
        return {
            r[0] for r in conn.execute("SELECT delegation_id FROM async_delegations")
        }


class _RecordingExecutor:
    def __init__(self, sink):
        self._sink = sink

    def submit(self, fn, *args, **kwargs):
        self._sink.append(fn)
        return None


@pytest.fixture
def spawned(monkeypatch):
    sink: list = []
    monkeypatch.setattr(
        async_delegation, "_get_executor", lambda n: _RecordingExecutor(sink)
    )
    return sink


def _dispatch(**kw):
    base = dict(
        goal="rebuild the index",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="sk",
        runner=lambda: {"status": "completed"},
    )
    base.update(kw)
    return async_delegation.dispatch_async_delegation(**base)


def _dispatch_batch(**kw):
    base = dict(
        goals=["a", "b"],
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="sk",
        runner=lambda: {"results": []},
    )
    base.update(kw)
    return async_delegation.dispatch_async_delegation_batch(**base)


def _sealed_receipt():
    """A genuinely minted, genuinely sealed receipt — the strongest input the
    registry could ever be handed. It must still not buy a dispatch.

    Minted directly because no admission path releases one: K8 compares public
    AuthorityPin fields only, so it never authenticates an authority and never
    hands out a capability. This is therefore strictly stronger than anything
    production can produce.
    """
    return durable_admission._mint_receipt(
        durable_admission._MINT,
        work_id="01a061a7-cea0-7503-b308-1f4029d450c8",
        idempotency_key="a" * 64,
        seam="execution",
        content_sha256="b" * 64,
        request_key="k8-real",
        admitted_at=0,
        authority_pin_matched=True,
    )


# --------------------------------------------------------------------------- #
# RED — a kill leaves an execution nobody can name
# --------------------------------------------------------------------------- #


@requires_k7
def test_red_armed_registry_accepts_no_session_bound_long_work(
    monkeypatch, tmp_path, spawned
):
    """Armed, this registry must accept no long work at all — so a kill can
    never leave a row nobody can reconcile.

    RED on unfixed code: the dispatch is accepted, a row is written and a
    thread is submitted, and a dead owner recovers as ``unknown`` naming
    nothing. Called with the PRE-EXISTING signature so this fails on behaviour,
    not on a TypeError.
    """
    _arm(monkeypatch, tmp_path)

    result = _dispatch()

    assert result["status"] == "rejected"
    assert result["reason"] == "session_bound_dispatch_refused"
    assert _rows() == set(), "a durable row was written for refused work"
    assert spawned == [], "a worker thread was submitted for refused work"


# --------------------------------------------------------------------------- #
# Registry forgery — no receipt buys a session-bound dispatch
# --------------------------------------------------------------------------- #


@requires_k7
@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"work_id": "fake"},
        {"work_id": "01a061a7-cea0-7503-b308-1f4029d450c8"},
        {"work_id": "01a061a7-cea0-7503-b308-1f4029d450c8", "seal": "x" * 64},
        "01a061a7-cea0-7503-b308-1f4029d450c8",
    ],
    ids=["none", "fake", "plausible-dict", "forged-seal", "bare-string"],
)
def test_single_dispatch_refuses_every_unsealed_receipt(
    monkeypatch, tmp_path, spawned, receipt
):
    _arm(monkeypatch, tmp_path)

    result = _dispatch(work_receipt=receipt)

    assert result["status"] == "rejected"
    assert result["reason"] == "session_bound_dispatch_refused"
    assert _rows() == set()
    assert spawned == []


@requires_k7
@pytest.mark.parametrize(
    "receipt",
    [None, {"work_id": "fake"}, {"work_id": "x", "seal": "y" * 64}],
    ids=["none", "fake", "forged-seal"],
)
def test_batch_dispatch_refuses_every_unsealed_receipt(
    monkeypatch, tmp_path, spawned, receipt
):
    _arm(monkeypatch, tmp_path)

    result = _dispatch_batch(work_receipt=receipt)

    assert result["status"] == "rejected"
    assert result["reason"] == "session_bound_dispatch_refused"
    assert _rows() == set()
    assert spawned == []


@requires_k7
def test_even_a_genuinely_sealed_receipt_buys_no_dispatch(monkeypatch, tmp_path, spawned):
    """The registry is refused because of WHAT it is, not because the caller
    failed to prove something."""
    _arm(monkeypatch, tmp_path)
    receipt = _sealed_receipt()
    assert durable_admission.verify_work_receipt(receipt) is receipt

    assert _dispatch(work_receipt=receipt)["status"] == "rejected"
    assert _dispatch_batch(work_receipt=receipt)["status"] == "rejected"
    assert _rows() == set()
    assert spawned == []


@requires_k7
def test_refusal_happens_before_the_capacity_check(monkeypatch, tmp_path, spawned):
    """Ordering matters: refusing after the capacity check would mean a full
    pool produced a different, weaker refusal."""
    _arm(monkeypatch, tmp_path)

    result = _dispatch(max_async_children=0)

    assert result["reason"] == "session_bound_dispatch_refused"
    assert "capacity" not in result["error"].lower()


# --------------------------------------------------------------------------- #
# Mode off — the pre-K8 registry, untouched, unmigrated
# --------------------------------------------------------------------------- #


def test_mode_off_dispatch_is_unchanged(monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)
    release = threading.Event()
    release.set()

    result = _dispatch(runner=lambda: {"status": "completed"})

    assert result["status"] == "dispatched"
    assert result["delegation_id"]


def test_mode_off_batch_dispatch_is_unchanged(monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)

    assert _dispatch_batch(runner=lambda: {"results": []})["status"] == "dispatched"


def test_mode_off_adds_no_k8_column_to_the_ledger(monkeypatch):
    """K8 dispatches nothing here, so it migrates nothing. A mode-off process
    must leave the ledger's shape exactly as it found it."""
    monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)
    _dispatch(runner=lambda: {"status": "completed"})

    with async_delegation._DB_LOCK, async_delegation._transaction() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(async_delegations)")}

    assert "work_id" not in columns


def test_mode_off_capacity_rejection_keeps_its_shape(monkeypatch):
    monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)
    release = threading.Event()

    def _blocking():
        release.wait(timeout=5)
        return {"status": "completed"}

    first = _dispatch(runner=_blocking, max_async_children=1)
    second = _dispatch(runner=_blocking, max_async_children=1)
    release.set()

    assert first["status"] == "dispatched"
    assert second["status"] == "rejected"
    assert "capacity" in second["error"].lower()
    assert "reason" not in second


# --------------------------------------------------------------------------- #
# delegate_task: the model-facing surface
# --------------------------------------------------------------------------- #


class _ParentAgent:
    def __init__(self, session_id="k8-session-alpha", depth=0):
        self.session_id = session_id
        self.platform = "cli"
        self._delegate_depth = depth
        self.model = "m"
        self.provider = "p"
        self.base_url = "https://example.invalid"
        self.api_key = "k"
        self.api_mode = "chat_completions"
        self.quiet_mode = True
        self._interrupt_requested = False
        self._active_children = []
        self._active_children_lock = threading.Lock()
        self._session_db = None


def _no_children(monkeypatch):
    """Any child construction is a failure on the handoff path."""
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool,
        "_build_child_agent",
        lambda **kw: (_ for _ in ()).throw(AssertionError("a child was built")),
    )


def _watch_live_transcripts(monkeypatch, sink):
    import tools.delegation_live_log as live_log

    real = live_log.create_live_transcripts
    monkeypatch.setattr(
        live_log,
        "create_live_transcripts",
        lambda *a, **kw: sink.append(a) or real(*a, **kw),
    )


@requires_k7
def test_background_delegation_is_handed_off_not_dispatched(monkeypatch, tmp_path, spawned):
    """No child, no thread, no row, and nothing that reads as running."""
    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    _no_children(monkeypatch)
    transcripts: list = []
    _watch_live_transcripts(monkeypatch, transcripts)

    payload = json.loads(
        delegate_tool.delegate_task(
            goal="audit the whole repo", background=True, parent_agent=_ParentAgent()
        )
    )

    assert payload["status"] == "handoff_required"
    assert payload["handoff"]["dispatcher"] == durable_admission.EXTERNAL_WORK_DISPATCHER
    assert payload.get("mode") != "background"
    assert _rows() == set()
    assert spawned == []
    assert transcripts == [], "live transcripts were created for handed-off work"
    blob = json.dumps(payload).lower()
    assert "dispatched" not in blob
    assert "running in the background" not in blob
    assert "resume" not in blob


@requires_k7
def test_handoff_is_stable_across_calls(monkeypatch, tmp_path):
    """A stable typed refusal, not an arbitrary error the model must interpret."""
    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    _no_children(monkeypatch)

    first = json.loads(
        delegate_tool.delegate_task(goal="g", background=True, parent_agent=_ParentAgent())
    )
    second = json.loads(
        delegate_tool.delegate_task(goal="g", background=True, parent_agent=_ParentAgent())
    )

    assert first["status"] == second["status"] == "handoff_required"
    assert first["handoff"]["dispatcher"] == second["handoff"]["dispatcher"]
    assert first["handoff"]["request_key"] == second["handoff"]["request_key"]


@requires_k7
def test_handoff_identity_separates_same_goal_different_context(monkeypatch, tmp_path):
    """The same goal with a different context is a different execution."""
    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    _no_children(monkeypatch)

    a = json.loads(
        delegate_tool.delegate_task(
            goal="audit", context="repo A", background=True, parent_agent=_ParentAgent()
        )
    )
    b = json.loads(
        delegate_tool.delegate_task(
            goal="audit", context="repo B", background=True, parent_agent=_ParentAgent()
        )
    )

    assert a["handoff"]["request_key"] != b["handoff"]["request_key"]


@requires_k7
def test_sync_delegation_without_an_inherited_admission_is_refused(
    monkeypatch, tmp_path
):
    """A synchronous fan-out may only run inside an execution the ledger has
    already admitted."""
    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    _no_children(monkeypatch)

    payload = json.loads(
        delegate_tool.delegate_task(
            goal="look something up", background=False, parent_agent=_ParentAgent()
        )
    )

    assert payload["status"] == "admission_required"
    assert payload["reason"] == "no_inherited_admission"
    assert "nothing was started" in payload["error"].lower()


@requires_k7
def test_nested_orchestrator_inherits_and_opens_no_second_work(monkeypatch, tmp_path):
    """A nested orchestrator delegation runs under the turn's admission. It must
    not admit again — that would be a second Work for one execution."""
    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    admitted = durable_admission.admit_input(
        session_id="k8-session-alpha",
        request_text="do the thing",
        event_id="evt.cli.nested",
    )
    durable_admission.bind_admitted_turn(admitted)
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **kw: MagicMock())
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda task_index, goal, child=None, parent_agent=None, **kw: {
            "task_index": task_index, "goal": goal,
            "status": "completed", "summary": "ok",
        },
    )
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *a, **k: {
            "model": "m", "provider": None, "base_url": None, "api_key": None,
            "api_mode": None, "command": None, "args": None,
        },
    )
    spawned_admissions: list = []
    real_admit = durable_admission.admit_execution
    monkeypatch.setattr(
        durable_admission,
        "admit_execution",
        lambda **kw: spawned_admissions.append(kw) or real_admit(**kw),
    )

    payload = json.loads(
        delegate_tool.delegate_task(
            goal="sub-task", background=False, parent_agent=_ParentAgent(depth=1)
        )
    )

    assert "results" in payload, payload
    assert payload["results"][0]["status"] == "completed"
    assert spawned_admissions == [], "a nested sync subtask opened its own Work"


@requires_k7
def test_a_blocked_turn_leaves_nothing_to_inherit(monkeypatch, tmp_path):
    """The inheritance token is bound per turn, so a refused turn cannot leave a
    previous turn's admission behind for a tool to ride."""
    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    durable_admission.bind_admitted_turn(None)
    _no_children(monkeypatch)

    payload = json.loads(
        delegate_tool.delegate_task(
            goal="anything", background=False, parent_agent=_ParentAgent()
        )
    )

    assert payload["status"] == "admission_required"


def test_delegate_task_mode_off_is_unchanged(monkeypatch, tmp_path):
    """Unarmed, the background path walks straight into child construction."""
    from tools import delegate_tool

    monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)

    def _boom(*a, **kw):
        raise AssertionError("mode off must not spawn the admitter")

    monkeypatch.setattr(durable_admission.subprocess, "run", _boom)
    monkeypatch.setattr(
        delegate_tool,
        "_build_child_agent",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("reached child construction")),
    )

    with pytest.raises(RuntimeError, match="reached child construction"):
        delegate_tool.delegate_task(
            goal="g", background=True, parent_agent=_ParentAgent()
        )


# --------------------------------------------------------------------------- #
# Authority is a candidate, never a verification
# --------------------------------------------------------------------------- #


@requires_k7
def test_the_handoff_declares_that_verification_is_still_owed(monkeypatch, tmp_path):
    """K8 compares public pin fields only, so whatever it hands over is an
    unauthenticated candidate and the payload says so."""
    from tools import delegate_tool

    _arm(monkeypatch, tmp_path)
    _no_children(monkeypatch)

    payload = json.loads(
        delegate_tool.delegate_task(
            goal="audit the repo", background=True, parent_agent=_ParentAgent()
        )
    )

    handoff = payload["handoff"]
    assert handoff["authority_verified"] is False
    assert handoff["authority_verification_required"] is True


@requires_k7
def test_the_registry_refuses_a_candidate_receipt_too(monkeypatch, tmp_path, spawned):
    """Fake, sealed and candidate receipts alike: refused before row, thread,
    child or ACK."""
    _arm(monkeypatch, tmp_path)
    candidate = durable_admission._mint_receipt(
        durable_admission._MINT,
        work_id="01a061a7-cea0-7503-b308-1f4029d450c8",
        idempotency_key="a" * 64,
        seam="execution",
        content_sha256="b" * 64,
        request_key="k8-candidate",
        admitted_at=0,
        authority_pin_matched=True,
    )

    for result in (
        _dispatch(work_receipt=candidate),
        _dispatch_batch(work_receipt=candidate),
    ):
        assert result["status"] == "rejected"
        assert result["reason"] == "session_bound_dispatch_refused"
    assert _rows() == set()
    assert spawned == []
