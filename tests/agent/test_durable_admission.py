"""K8: no model token, and no promise, before the K7 kernel has admitted it.

Every test that needs a real decision drives the REAL cross-repo K7 admitter
(``admit_cli.py``) as a subprocess, pinned by the exact SHA-256 this consumer
was approved against. K7's scripted ``--transport-fixture`` is its own offline
seam (it refuses it unless ``HERMES_KERNEL_TEST_FIXTURE=1`` reaches the child)
and everything minted through it speaks for ``test_fixture`` — which is why the
authority tests below assert that such a receipt can never release an effect.

Two cooperative stand-in adapters appear here (a slow one, a lying one). They
exercise K8's OWN refusals — the frozen outer budget and the receipt checks —
which the real kernel cannot be asked to produce. Neither is ever the only
evidence for a contract: the real CLI covers the same ground beside them.
"""

from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path

import pytest

from agent import conversation_loop, durable_admission

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


def test_observe_origin_is_sealed_scoped_and_never_admits(monkeypatch):
    from dataclasses import FrozenInstanceError, replace

    monkeypatch.setenv(durable_admission.ENV_MODE, "observe")
    monkeypatch.setattr(durable_admission, "_settings", lambda: {
        "tenant": "personal", "machine": "mini",
    })
    monkeypatch.setattr(durable_admission, "_active_profile", lambda: "projetospessoais")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ingress subprocess"))
    assert durable_admission.resolve_mode() is False
    origin = durable_admission.capture_effect_origin("chat-original", "telegram:123")
    assert origin is not None
    with pytest.raises(FrozenInstanceError):
        origin.event_id = "telegram:456"
    assert durable_admission.capture_effect_origin("chat-original", "") is None
    with durable_admission.effect_origin_scope(origin):
        assert durable_admission.current_effect_origin() is origin
        with pytest.raises(RuntimeError):
            with durable_admission.effect_origin_scope(None):
                assert durable_admission.current_effect_origin() is None
                raise RuntimeError("unwind")
        assert durable_admission.current_effect_origin() is origin
        with pytest.raises(ValueError):
            with durable_admission.effect_origin_scope(replace(origin, event_id="telegram:456")):
                pass
    assert durable_admission.current_effect_origin() is None
    monkeypatch.setattr(durable_admission, "_active_profile", lambda: "business")
    assert durable_admission.capture_effect_origin("chat-original", "telegram:123") is None


def test_observation_writer_reserves_once_and_latches_first_failure(tmp_path, monkeypatch):
    monkeypatch.setenv(durable_admission.ENV_MODE, "observe")
    monkeypatch.setattr(durable_admission, "_settings", lambda: {"tenant": "personal", "machine": "mini"})
    monkeypatch.setattr(durable_admission, "_active_profile", lambda: "projetospessoais")
    root = tmp_path / "receipts"
    writer = durable_admission.ObservationWriter(root, code_sha="a" * 40)
    writer.checkpoint(now=1000)
    origin = durable_admission.capture_effect_origin("chat-original", "telegram:123")
    first = writer.reserve(origin, "destination", "secret outgoing body", now=1001)
    assert first["state"] == "reserved" and first["sequence"] == 1
    assert writer.reserve(origin, "destination", "secret outgoing body", now=1002) == first
    second = writer.reserve(durable_admission.capture_effect_origin("chat-original", "telegram:124"),
                            "destination", "secret outgoing body", now=1002)
    assert second["effect_id"] != first["effect_id"] and second["sequence"] == 2
    writer.transition(first["effect_id"], "gap", gap_reason="ack_missing", now=1003)
    replay = writer.reserve(origin, "destination", "secret outgoing body", now=1004)
    assert replay["state"] == "gap" and replay["sequence"] == 1
    with pytest.raises(ValueError):
        writer.transition(first["effect_id"], "gap", gap_reason="release_failed", now=1004)
    writer.checkpoint(now=1005)
    channel = json.loads((root / "observation-channel.json").read_text())
    assert channel["last_effect_sequence"] == 2 and channel["valid_until"] == 1125
    assert "secret outgoing body" not in "".join(p.read_text() for p in root.rglob("*.json"))
    monkeypatch.setattr(writer, "_write", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        writer.checkpoint(now=1006)
    assert writer.broken is True


def test_observation_channel_rejects_a_second_live_writer(tmp_path):
    root = tmp_path / "channel-owner"
    first = durable_admission.ObservationWriter(root, code_sha="a" * 40)
    second = durable_admission.ObservationWriter(root, code_sha="a" * 40)
    first.checkpoint()
    before = (root / "observation-channel.json").read_bytes()
    try:
        with pytest.raises(BlockingIOError):
            second.checkpoint()
        assert (root / "observation-channel.json").read_bytes() == before
    finally:
        first.checkpoint(stopped=True)


def test_observation_channel_lock_excludes_another_process(tmp_path):
    import sys
    root = tmp_path / "process-channel"
    first = durable_admission.ObservationWriter(root, code_sha="a" * 40)
    first.checkpoint()
    before = (root / "observation-channel.json").read_bytes()
    code = """
import sys
from pathlib import Path
from agent.durable_admission import ObservationWriter
writer = ObservationWriter(Path(sys.argv[1]), code_sha='a' * 40)
try:
    writer.checkpoint()
except BlockingIOError:
    sys.exit(73)
writer.checkpoint(stopped=True)
"""
    try:
        refused = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, timeout=15)
        assert refused.returncode == 73 and refused.stdout == b""
        assert (root / "observation-channel.json").read_bytes() == before
    finally:
        first.checkpoint(stopped=True)
    successor = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, timeout=15)
    assert successor.returncode == 0 and successor.stdout == b""


def test_observation_sequence_across_boots_and_concurrent_reservations(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv(durable_admission.ENV_MODE, "observe")
    monkeypatch.setattr(durable_admission, "_settings", lambda: {"tenant": "personal", "machine": "mini"})
    monkeypatch.setattr(durable_admission, "_active_profile", lambda: "projetospessoais")
    root = tmp_path / "receipts"
    writer = durable_admission.ObservationWriter(root, code_sha="a" * 40)
    writer.checkpoint(now=1000)
    origin = durable_admission.capture_effect_origin("chat-original", "telegram:123")
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: writer.reserve(origin, "peer", "outgoing", now=1001), range(12)))
    assert all(record == records[0] for record in records)
    writer.checkpoint(now=1002, stopped=True)
    writer.checkpoint(now=1003)
    assert json.loads((root / "observation-channel.json").read_text())["state"] == "stopped"
    reboot = durable_admission.ObservationWriter(root, code_sha="a" * 40)
    reboot.checkpoint(now=1004)
    channel = json.loads((root / "observation-channel.json").read_text())
    assert channel["sequence"] == 4 and channel["state"] == "starting"
    assert channel["last_effect_sequence"] == 0
    assert reboot.reserve(origin, "peer", "outgoing", now=1005) == records[0]


class ReachedTurnContext(RuntimeError):
    """Probe marker: control reached the first model-facing step of the turn."""


@pytest.fixture(autouse=True)
def _clean_admission_state():
    durable_admission.reset_state_for_tests()
    yield
    durable_admission.reset_state_for_tests()


@pytest.fixture
def turn_probe(monkeypatch):
    """Stand in for ``build_turn_context`` — the turn's first model-facing step.

    It owns preflight compression, the oversized-resume rebuild and the
    ``pre_llm_call`` hook, so reaching it means a compressor or a provider is
    about to see the input. Recording the reach and raising keeps the assertion
    about control flow, not about a mocked answer.
    """
    reached: list = []

    def _probe(agent, user_message, *args, **kwargs):
        reached.append(user_message)
        raise ReachedTurnContext("build_turn_context reached")

    monkeypatch.setattr(conversation_loop, "build_turn_context", _probe)
    return reached


def _agent(session_id="k8-session-alpha", **extra):
    agent = types.SimpleNamespace(
        session_id=session_id,
        platform="cli",
        api_mode="chat_completions",
        _delegate_depth=0,
    )
    for key, value in extra.items():
        setattr(agent, key, value)
    return agent


def _write_config(tmp_path: Path, admission: dict) -> None:
    """Write a real ``config.yaml`` under the test's isolated HERMES_HOME."""
    import yaml

    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"durable_admission": admission}}), encoding="utf-8"
    )
    from hermes_cli.config import _LOAD_CONFIG_CACHE, _RAW_CONFIG_CACHE

    _LOAD_CONFIG_CACHE.clear()
    _RAW_CONFIG_CACHE.clear()


def _fixture(tmp_path: Path, responses: list, name="fixture.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"responses": responses}), encoding="utf-8")
    return path


def _pin_env(monkeypatch, binary=None, *, mode="enforce", schema_sha=None):
    """Set the full pin set. ``mode`` is what actually arms K8."""
    if mode is not None:
        monkeypatch.setenv("HERMES_KERNEL_V1_MODE", mode)
    else:
        monkeypatch.delenv("HERMES_KERNEL_V1_MODE", raising=False)
    monkeypatch.setenv("HERMES_KERNEL_ADMITTER_BIN", str(binary or K7_BIN))
    monkeypatch.setenv("HERMES_KERNEL_SCHEMA_PATH", str(K7_SCHEMA))
    monkeypatch.setenv(
        "HERMES_KERNEL_SCHEMA_SHA256", schema_sha or durable_admission.SCHEMA_SHA256
    )
    monkeypatch.setenv("HERMES_KERNEL_TEST_FIXTURE", "1")


def _arm_real_kernel(monkeypatch, tmp_path, responses=(OK_RESPONSE,), **kw):
    _pin_env(monkeypatch, **kw)
    _write_config(
        tmp_path,
        {
            "transport_fixture": str(_fixture(tmp_path, list(responses))),
            "state_dir": str(tmp_path / "kernel-state"),
        },
    )


def _adapter(tmp_path: Path, body: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _slow_adapter(tmp_path: Path) -> Path:
    """Outlives the frozen outer budget on purpose."""
    return _adapter(tmp_path, "import time\ntime.sleep(30)\n", "slow_admitter.py")


def _lying_adapter(tmp_path: Path, receipt: dict, exit_code: int = 0) -> Path:
    """An adapter that echoes our envelope back and then lies about the rest.

    Echoing matters: without it every receipt is refused as
    ``receipt_envelope_mismatch`` and the authority tests would pass for the
    wrong reason, never reaching the check they exist to exercise. The test's
    own fields are applied ON TOP, so a deliberate echo mutation still wins.
    """
    return _adapter(
        tmp_path,
        "import json, sys\n"
        "env = json.loads(sys.stdin.read())\n"
        "doc = {\n"
        "    'content_sha256': env['content_sha256'],\n"
        "    'tenant': env['tenant'],\n"
        "    'profile': env['profile'],\n"
        "    'origin_surface': env['origin']['surface'],\n"
        "    'origin_session_id': env['origin']['session_id'],\n"
        "    'origin_machine': env['origin']['machine'],\n"
        "    'source_event_kind': env['source_event']['kind'],\n"
        "    'source_event_id': env['source_event']['event_id'],\n"
        "    'binding': env.get('binding'),\n"
        "}\n"
        f"doc.update({receipt!r})\n"
        "print(json.dumps(doc))\n"
        f"raise SystemExit({exit_code})\n",
        "lying_admitter.py",
    )


def _arm_adapter(monkeypatch, tmp_path, adapter: Path, config=None):
    """Arm K8 against a stand-in adapter.

    The frozen admitter hash cannot match a stand-in, so the pin is relaxed for
    exactly these tests — they exist to exercise the checks that happen AFTER
    the binary is trusted, and the pin itself is covered by its own test below.
    """
    _pin_env(monkeypatch, adapter)
    monkeypatch.setattr(
        durable_admission, "ADMITTER_SHA256", durable_admission._sha256_file(adapter)
    )
    _write_config(tmp_path, config or {"state_dir": str(tmp_path / "st")})


def _receipt(**overrides) -> dict:
    base = {
        "schema_version": "hermes-kernel-admission.v1",
        "kernel_version": "hermes-kernel.v1",
        "envelope_schema_version": "work-envelope.v1",
        "schema_sha256": durable_admission.SCHEMA_SHA256,
        "idempotency_key": "a" * 64,
        "state": "admitted",
        "reason_code": "execution_seam_requested",
        "seam": "execution",
        "work_id": "01a061a7-cea0-7503-b308-1f4029d450c8",
        "effects_allowed": True,
        "model_may_run": True,
        "ensure_work_required": True,
        "signal_persisted_before_model": True,
        "replayed": False,
    }
    base.update(overrides)
    return base


def _admit(text="mutate something", session="k8-session-alpha", event="evt.cli.a1"):
    return durable_admission.admit_input(
        session_id=session, request_text=text, event_id=event, declares_execution=True
    )


# --------------------------------------------------------------------------- #
# RED — the defects this package exists to remove
# --------------------------------------------------------------------------- #


@requires_k7
def test_red_natural_message_reaches_the_model_path_with_no_signal(
    tmp_path, monkeypatch, turn_probe
):
    """A natural message must not reach the turn's first model-facing step
    before its Signal is durable.

    RED on unfixed code: ``run_conversation`` walks straight into
    ``build_turn_context`` — preflight compression and the ``pre_llm_call``
    hook — and the kernel's inbox does not exist, so nothing recorded that the
    input happened.
    """
    _arm_real_kernel(monkeypatch, tmp_path)

    with pytest.raises(ReachedTurnContext):
        conversation_loop.run_conversation(_agent(), "ship the release")

    assert turn_probe == ["ship the release"]
    assert (tmp_path / "kernel-state" / "inbox.db").is_file(), (
        "the turn reached build_turn_context with no Signal recorded"
    )


@requires_k7
def test_red_unreachable_admitter_still_runs_the_turn(tmp_path, monkeypatch, turn_probe):
    """A dead admitter must stop the turn.

    RED on unfixed code: nothing consults the admitter, so a dead one changes
    nothing and the model runs regardless.
    """
    _arm_real_kernel(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KERNEL_ADMITTER_BIN", str(tmp_path / "not-there.py"))

    result = conversation_loop.run_conversation(_agent(), "ship the release")

    assert turn_probe == []
    assert result["admission_blocked"] is True
    assert result["completed"] is False


# --------------------------------------------------------------------------- #
# Default-off — arming is explicit, and off costs nothing
# --------------------------------------------------------------------------- #


def test_admitter_path_alone_does_not_arm(monkeypatch, tmp_path):
    """A path says where a kernel lives, not that we decided to enforce with it."""
    _pin_env(monkeypatch, mode=None)

    assert durable_admission.admission_enabled() is False


@pytest.mark.parametrize("mode", ["off", "0", "false", "disabled"])
def test_explicit_off_wins_over_a_full_pin_set(monkeypatch, mode):
    _pin_env(monkeypatch, mode=mode)

    assert durable_admission.admission_enabled() is False


def test_unrecognised_mode_fails_closed_before_any_side_effect(monkeypatch, tmp_path):
    """A typo must not read as "off".

    An operator who wrote ``enfroce`` asked for enforcement; silently giving
    them none is a fail-open. The refusal is stable and lands before any config
    read, subprocess or model call.
    """
    _pin_env(monkeypatch, mode="enfroce")
    import hermes_cli.config as config_mod

    def _no_config(*a, **kw):
        raise AssertionError("a bad mode must not reach config.yaml")

    def _no_subprocess(*a, **kw):
        raise AssertionError("a bad mode must not spawn the admitter")

    monkeypatch.setattr(config_mod, "load_config_readonly", _no_config)
    monkeypatch.setattr(durable_admission.subprocess, "run", _no_subprocess)

    with pytest.raises(durable_admission.ModeError):
        durable_admission.resolve_mode()

    outcome = _admit()
    assert outcome.reason_code == "kernel_mode_invalid"
    assert outcome.model_may_run is False
    assert outcome.effects_allowed is False


def test_unrecognised_mode_blocks_the_turn(monkeypatch, tmp_path, turn_probe):
    _pin_env(monkeypatch, mode="enfroce")

    result = conversation_loop.run_conversation(_agent(), "anything")

    assert turn_probe == []
    assert result["admission_blocked"] is True
    assert result["admission_reason_code"] == "kernel_mode_invalid"


def test_fully_pinned_but_off_touches_nothing(monkeypatch, tmp_path, turn_probe):
    """Explicitly off, with every pin present, must be inert.

    No config read, no subprocess, no database open, no notice change — the
    pre-K8 turn, byte for byte.
    """
    _pin_env(monkeypatch, mode="off")

    def _no_subprocess(*a, **kw):
        raise AssertionError("mode off must not spawn the admitter")

    def _no_config(*a, **kw):
        raise AssertionError("mode off must not read config.yaml")

    def _no_sqlite(*a, **kw):
        raise AssertionError("mode off must not open a database")

    import hermes_cli.config as config_mod
    import sqlite3

    monkeypatch.setattr(durable_admission.subprocess, "run", _no_subprocess)
    monkeypatch.setattr(config_mod, "load_config_readonly", _no_config)
    monkeypatch.setattr(config_mod, "load_config", _no_config)
    monkeypatch.setattr(sqlite3, "connect", _no_sqlite)

    assert durable_admission.admission_enabled() is False
    with pytest.raises(ReachedTurnContext):
        conversation_loop.run_conversation(_agent(), "business as usual")

    assert turn_probe == ["business as usual"]


@requires_k7
def test_config_mode_off_disarms_an_armed_env(monkeypatch, tmp_path, turn_probe):
    _arm_real_kernel(monkeypatch, tmp_path)
    _write_config(tmp_path, {"mode": "off", "state_dir": str(tmp_path / "kernel-state")})

    with pytest.raises(ReachedTurnContext):
        conversation_loop.run_conversation(_agent(), "business as usual")

    assert turn_probe == ["business as usual"]
    assert not (tmp_path / "kernel-state").exists()


# --------------------------------------------------------------------------- #
# Trust roots — the environment says WHERE, the code says WHICH
# --------------------------------------------------------------------------- #


@requires_k7
def test_the_pinned_admitter_hash_is_the_approved_k7_build():
    """The constant this consumer ships must be the hash of the real binary."""
    expected_sha256 = (
        durable_admission.TURN_IDENTITY_ADMITTER_SHA256
        if os.environ.get("HERMES_TEST_K7_ROOT")
        else durable_admission.ADMITTER_SHA256
    )
    assert (
        durable_admission._sha256_file(K7_BIN) == expected_sha256
    )
    assert (
        durable_admission._sha256_file(K7_SCHEMA) == durable_admission.SCHEMA_SHA256
    )


def test_admitter_hash_mismatch_refuses_before_executing(monkeypatch, tmp_path):
    """A different binary is a different kernel, whatever the path says — and it
    is refused BEFORE it runs, not after reading its answer."""
    impostor = _adapter(tmp_path, "print('{}')\n", "impostor.py")
    _pin_env(monkeypatch, impostor)
    _write_config(tmp_path, {"state_dir": str(tmp_path / "st")})
    spawned = []
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda *a, **kw: spawned.append(a) or (_ for _ in ()).throw(AssertionError()),
    )

    outcome = _admit()

    assert outcome.reason_code == "admitter_hash_mismatch"
    assert outcome.model_may_run is False
    assert spawned == [], "the impostor binary was executed before being refused"


@requires_k7
def test_env_schema_pin_must_match_the_frozen_constant(monkeypatch, tmp_path):
    """The environment may say where the contract is; it may not say which
    contract this consumer agreed to."""
    _arm_real_kernel(monkeypatch, tmp_path, schema_sha="b" * 64)

    outcome = _admit()

    assert outcome.reason_code == "schema_pin_not_frozen"
    assert outcome.model_may_run is False


@requires_k7
def test_schema_file_hash_is_verified_against_the_pin(monkeypatch, tmp_path):
    _arm_real_kernel(monkeypatch, tmp_path)
    other = tmp_path / "other-schema.json"
    other.write_text('{"not": "the pinned contract"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_KERNEL_SCHEMA_PATH", str(other))

    outcome = _admit()

    assert outcome.reason_code == "schema_hash_mismatch"
    assert outcome.effects_allowed is False


def test_outer_budget_is_frozen_and_config_cannot_raise_it(monkeypatch, tmp_path):
    """The 2.0s outer wait is a module constant with no config override."""
    assert durable_admission.TIMEOUT_SECONDS == 2.0

    adapter = _adapter(tmp_path, "import sys\nsys.stdin.read()\nprint('{}')\n", "quick.py")
    _arm_adapter(
        monkeypatch, tmp_path, adapter, {"timeout_ms": 30000, "state_dir": str(tmp_path / "st")}
    )
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda argv, **kw: seen.update(kw) or real(argv, **kw),
    )

    _admit()

    assert seen["timeout"] == 2.0, "a config key raised the frozen outer budget"


def test_external_timeout_is_ambiguous_never_dispatched(monkeypatch, tmp_path):
    """K7's client owns its own remote timeout; this budget measures how long we
    wait on the subprocess. Expiring it says nothing about whether the kernel
    decided, so it is ambiguous and must not be re-asked from here."""
    _arm_adapter(monkeypatch, tmp_path, _slow_adapter(tmp_path))

    outcome = _admit()

    assert outcome.reason_code == "admitter_timeout"
    assert outcome.model_may_run is False
    assert outcome.effects_allowed is False
    assert outcome.retryable is False
    assert outcome.work_id is None


def test_user_text_cannot_steer_the_invocation(monkeypatch, tmp_path):
    """Argv is a list, there is no shell, and the input rides stdin — so text
    that looks like a flag stays text."""
    adapter = _adapter(tmp_path, "import sys\nsys.stdin.read()\nprint('{}')\n", "echo.py")
    _arm_adapter(monkeypatch, tmp_path, adapter)
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda argv, **kw: seen.update({"argv": argv, "kw": kw}) or real(argv, **kw),
    )

    hostile = "--seam execution; rm -rf / && echo $(whoami) `id`"
    _admit(text=hostile)

    assert isinstance(seen["argv"], list)
    assert seen["kw"].get("shell") is False
    assert all(hostile not in str(part) for part in seen["argv"])
    assert seen["argv"][0] == str(adapter)


def test_python_path_is_not_forwarded_to_the_admitter(monkeypatch, tmp_path):
    """A caller-set ``PYTHONPATH`` could shadow ``control.kernel`` inside the
    admitter, letting the thing being gated supply its own gate."""
    out = tmp_path / "child-env.json"
    adapter = _adapter(
        tmp_path,
        "import json, os, sys\nsys.stdin.read()\n"
        f"open({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "print('{}')\nraise SystemExit(4)\n",
        "env_probe.py",
    )
    _arm_adapter(monkeypatch, tmp_path, adapter)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "evil"))

    _admit()

    child_env = json.loads(out.read_text(encoding="utf-8"))
    assert "PYTHONPATH" not in child_env
    assert child_env.get("PATH")


# --------------------------------------------------------------------------- #
# Authority — a label is not an authorisation
# --------------------------------------------------------------------------- #

_PIN = {
    "authority_source": "remote",
    "authority_root_id": "dd63679a14e24e9d5472e2ab8ece3f27",
    "attestation_key_id": "0123456789abcdef",
    "secret_ref": "env://JARVIS_KEY",
}


def _attested(**over):
    attestation = {
        "alg": "hmac-sha256",
        "version": "hermes-kernel-admission-attestation.v1",
        "authority_source": "remote",
        "authority_root_id": _PIN["authority_root_id"],
        "key_id": _PIN["attestation_key_id"],
        "value": "c" * 64,
    }
    attestation.update(over.pop("attestation", {}))
    doc = _receipt(
        authority_source="remote",
        authority_root_id=_PIN["authority_root_id"],
        attestation=attestation,
    )
    doc.update(over)
    return doc


@requires_k7
def test_fixture_authority_never_authorizes_an_effect(monkeypatch, tmp_path):
    """The offline transport speaks for ``test_fixture``. A test double that can
    mint authority is not a test double — it is a bypass."""
    _arm_real_kernel(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        {
            "transport_fixture": str(_fixture(tmp_path, [OK_RESPONSE])),
            "state_dir": str(tmp_path / "kernel-state"),
            "authority_pin": dict(_PIN),
        },
    )

    outcome = _admit()

    assert outcome.state == "admitted"
    assert outcome.receipt["authority_source"] == "test_fixture"
    assert outcome.authority_pin_matched is False
    assert outcome.authority_verified is False
    assert outcome.effects_allowed is False
    assert outcome.has_work_receipt is False
    assert outcome.work_receipt() is None


def test_no_pin_configured_means_no_effect_is_ever_authorized(monkeypatch, tmp_path):
    """"The gate found no pin, so it trusted the receipt" is the exact defect
    the pin exists to remove."""
    _arm_adapter(monkeypatch, tmp_path, _lying_adapter(tmp_path, _attested()))

    outcome = _admit()

    assert outcome.kernel_effects_allowed is True
    assert outcome.authority_pin_matched is False
    assert outcome.authority_verified is False
    assert outcome.effects_allowed is False


@pytest.mark.parametrize(
    "mutation,label",
    [
        ({"authority_source": "test_fixture"}, "relabelled source"),
        ({"authority_root_id": "f" * 32}, "another authority root"),
        ({"attestation": None}, "no attestation at all"),
    ],
)
def test_receipts_that_do_not_match_the_pin_are_refused(
    monkeypatch, tmp_path, mutation, label
):
    doc = _attested()
    doc.update(mutation)
    _arm_adapter(
        monkeypatch,
        tmp_path,
        _lying_adapter(tmp_path, doc),
        {"state_dir": str(tmp_path / "st"), "authority_pin": dict(_PIN)},
    )

    outcome = _admit()

    assert outcome.authority_pin_matched is False, label
    assert outcome.effects_allowed is False


@pytest.mark.parametrize(
    "attestation_mutation",
    [
        {"key_id": "ffffffffffffffff"},
        {"alg": "none"},
        {"version": "hermes-kernel-admission-attestation.v0"},
        {"authority_source": "test_fixture"},
        {"value": "short"},
    ],
)
def test_attestation_that_is_not_the_pinned_key_is_refused(
    monkeypatch, tmp_path, attestation_mutation
):
    _arm_adapter(
        monkeypatch,
        tmp_path,
        _lying_adapter(tmp_path, _attested(attestation=attestation_mutation)),
        {"state_dir": str(tmp_path / "st"), "authority_pin": dict(_PIN)},
    )

    assert _admit().effects_allowed is False


@pytest.mark.parametrize(
    "bad_pin",
    [
        {"authority_source": "test_fixture", "authority_root_id": "d" * 32,
         "attestation_key_id": "0123456789abcdef", "secret_ref": "env://X"},
        {"authority_source": "remote", "authority_root_id": "nothex",
         "attestation_key_id": "0123456789abcdef", "secret_ref": "env://X"},
        {"authority_source": "remote", "authority_root_id": "d" * 32,
         "attestation_key_id": "0123456789abcdef"},
    ],
)
def test_an_incomplete_or_fixture_pin_authorizes_nothing(monkeypatch, tmp_path, bad_pin):
    """Pinning the offline domain, or pinning half a pin, must not become a
    weaker gate than pinning nothing."""
    _arm_adapter(
        monkeypatch,
        tmp_path,
        _lying_adapter(tmp_path, _attested()),
        {"state_dir": str(tmp_path / "st"), "authority_pin": bad_pin},
    )

    assert _admit().effects_allowed is False


# --------------------------------------------------------------------------- #
# Receipt integrity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"kernel_version": "hermes-kernel.v9"}, "kernel_version_mismatch"),
        ({"schema_version": "hermes-kernel-admission.v9"}, "kernel_version_mismatch"),
        ({"schema_sha256": "b" * 64}, "schema_hash_mismatch"),
        ({"content_sha256": "c" * 64}, "receipt_envelope_mismatch"),
        ({"origin_session_id": "someone-else"}, "receipt_envelope_mismatch"),
        ({"binding": {"request_key": "k8-someone-else"}}, "receipt_envelope_mismatch"),
        ({"state": "nonsense"}, "receipt_incoherent"),
    ],
)
def test_a_receipt_that_is_not_ours_is_refused(monkeypatch, tmp_path, mutation, expected):
    doc = _receipt()
    doc.update(mutation)
    _arm_adapter(monkeypatch, tmp_path, _lying_adapter(tmp_path, doc))

    outcome = _admit()

    assert outcome.reason_code == expected
    assert outcome.effects_allowed is False


def test_exit_code_and_state_must_agree(monkeypatch, tmp_path):
    """``admitted`` on the pending exit code is incoherent, and an incoherent
    receipt is refused rather than resolved in the caller's favour."""
    _arm_adapter(monkeypatch, tmp_path, _lying_adapter(tmp_path, _receipt(), exit_code=1))

    assert _admit().reason_code == "receipt_incoherent"


def test_admitter_crash_is_pending_not_permission(monkeypatch, tmp_path):
    _arm_adapter(
        monkeypatch,
        tmp_path,
        _adapter(tmp_path, "import sys\nsys.stdin.read()\nraise SystemExit(4)\n", "crash.py"),
    )

    outcome = _admit()

    assert outcome.model_may_run is False
    assert outcome.reason_code in ("admitter_internal_failure", "receipt_unreadable")


# --------------------------------------------------------------------------- #
# Identity — the platform's, not the text's
# --------------------------------------------------------------------------- #


@requires_k7
def test_replaying_one_platform_event_is_one_signal(monkeypatch, tmp_path):
    _arm_real_kernel(monkeypatch, tmp_path)

    first = _admit(event="evt.telegram.4212")
    second = _admit(event="evt.telegram.4212")

    assert first.idempotency_key == second.idempotency_key
    assert second.replayed is True


@requires_k7
def test_same_text_from_two_events_is_two_signals(monkeypatch, tmp_path):
    """Two different messages that happen to read the same are two inputs."""
    _arm_real_kernel(monkeypatch, tmp_path)

    first = _admit(text="ok", event="evt.telegram.1")
    second = _admit(text="ok", event="evt.telegram.2")

    assert first.idempotency_key != second.idempotency_key
    assert first.replayed is False and second.replayed is False


@requires_k7
def test_two_sessions_do_not_share_a_signal(monkeypatch, tmp_path):
    _arm_real_kernel(monkeypatch, tmp_path)

    alpha = _admit(text="same words", session="k8-session-alpha", event="evt.x.1")
    beta = _admit(text="same words", session="k8-session-beta", event="evt.x.1")

    assert alpha.idempotency_key != beta.idempotency_key


@requires_k7
def test_execution_identity_covers_context_role_and_schema(monkeypatch, tmp_path):
    """The same goal with a different context, role or output schema is a
    DIFFERENT execution and must not collapse onto the first one's Work."""
    _arm_real_kernel(monkeypatch, tmp_path)
    base = {"goals": [{"goal": "audit", "context": "repo A", "role": "leaf"}]}
    other = {"goals": [{"goal": "audit", "context": "repo B", "role": "leaf"}]}

    a = durable_admission.admit_execution(
        session_id="k8-session-alpha",
        request=base,
        event_id=durable_admission.execution_event_id("k8-session-alpha", base),
    )
    b = durable_admission.admit_execution(
        session_id="k8-session-alpha",
        request=other,
        event_id=durable_admission.execution_event_id("k8-session-alpha", other),
    )

    assert a.idempotency_key != b.idempotency_key


# --------------------------------------------------------------------------- #
# The observation-time sentinel — replay stability without fabricating a clock
# --------------------------------------------------------------------------- #


@requires_k7
def test_envelope_observation_time_is_the_sentinel_not_the_clock(monkeypatch, tmp_path):
    """The wall clock must never reach ``source_event.observed_at``.

    K7 compares the whole canonical envelope for a given idempotency key, so a
    clock there makes every redelivery a different envelope — which is a
    correctness bug, not a cosmetic one.
    """
    _arm_real_kernel(monkeypatch, tmp_path)
    sent = {}
    real = subprocess.run
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda argv, **kw: sent.update({"argv": argv, "input": kw.get("input")})
        or real(argv, **kw),
    )

    _admit(event="evt.telegram.4242")

    envelope = json.loads(sent["input"])
    assert envelope["source_event"]["observed_at"] == durable_admission.OBSERVED_AT_UNKNOWN
    # The clock still exists — in --now, which K7 records as recorded_at and
    # which is deliberately NOT part of the envelope.
    assert "--now" in sent["argv"]
    assert int(sent["argv"][sent["argv"].index("--now") + 1]) > 1_600_000_000


@requires_k7
def test_replay_under_the_sentinel_is_byte_identical_and_one_signal(
    monkeypatch, tmp_path
):
    """Same event id, unknown observation time, twice: ONE Signal.

    The two attempts have different recording clocks on purpose — that is what
    a real redelivery looks like — and the canonical envelope must be identical
    regardless.
    """
    _arm_real_kernel(monkeypatch, tmp_path)
    payloads: list = []
    real = subprocess.run
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda argv, **kw: payloads.append(kw.get("input")) or real(argv, **kw),
    )

    first = _admit(event="evt.telegram.4242")
    second = _admit(event="evt.telegram.4242")

    assert payloads[0] == payloads[1], "the canonical envelope was not byte-stable"
    assert first.idempotency_key == second.idempotency_key
    assert first.receipt["signal"]["created"] is True
    assert second.receipt["signal"]["created"] is False


@requires_k7
def test_an_event_recorded_under_the_sentinel_can_never_be_upgraded(
    monkeypatch, tmp_path
):
    """A Signal recorded as "time unknown" must not silently become "time X".

    Rewriting a recorded identity underneath the ledger is worse than a
    refusal, so the second attempt fails closed by name.
    """
    _arm_real_kernel(monkeypatch, tmp_path)
    first = _admit(event="evt.telegram.4242")
    assert first.model_may_run is True

    upgraded = durable_admission.admit_input(
        session_id="k8-session-alpha",
        request_text="mutate something",
        event_id="evt.telegram.4242",
        declares_execution=True,
        observed_at=1_756_800_000,
    )

    assert upgraded.reason_code == "signal_envelope_mismatch"
    assert upgraded.model_may_run is False
    assert upgraded.effects_allowed is False


@requires_k7
def test_a_deterministic_upstream_time_is_carried_when_supplied(monkeypatch, tmp_path):
    """A caller that genuinely holds an immutable upstream time may pass it, and
    a replay carrying the SAME value stays one Signal."""
    _arm_real_kernel(monkeypatch, tmp_path)

    first = durable_admission.admit_input(
        session_id="k8-session-alpha",
        request_text="hello",
        event_id="evt.telegram.777",
        observed_at=1_756_800_000,
    )
    second = durable_admission.admit_input(
        session_id="k8-session-alpha",
        request_text="hello",
        event_id="evt.telegram.777",
        observed_at=1_756_800_000,
    )

    assert first.idempotency_key == second.idempotency_key
    assert first.receipt["signal"]["created"] is True
    assert second.receipt["signal"]["created"] is False


@pytest.mark.parametrize("bad", [-1, "0", 1.5, True, None])
def test_a_non_negative_integer_is_required_for_observation_time(
    monkeypatch, tmp_path, bad
):
    _arm_adapter(monkeypatch, tmp_path, _lying_adapter(tmp_path, _receipt()))

    outcome = durable_admission.admit_input(
        session_id="k8-session-alpha",
        request_text="hello",
        event_id="evt.cli.1",
        observed_at=bad,
    )

    assert outcome.reason_code == "envelope_invalid"
    assert outcome.model_may_run is False


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_platform_identity_has_no_event_id(raw):
    assert durable_admission.platform_event_id("telegram", raw) is None


@requires_k7
def test_armed_admission_without_event_identity_fails_closed(monkeypatch, tmp_path):
    _arm_real_kernel(monkeypatch, tmp_path)

    outcome = durable_admission.admit_input(
        session_id="k8-session-alpha", request_text="hello", event_id=""
    )

    assert outcome.reason_code == "envelope_invalid"
    assert outcome.model_may_run is False


@requires_k7
def test_input_above_the_envelope_cap_fails_closed_by_name(
    monkeypatch, tmp_path, turn_probe
):
    """An input above the frozen cap cannot be represented, so it is refused by
    name rather than truncated into a different input."""
    _arm_real_kernel(monkeypatch, tmp_path)

    result = conversation_loop.run_conversation(_agent(), "x" * 200_000)

    assert turn_probe == []
    assert result["admission_reason_code"] == "envelope_invalid"
    assert "input_too_large" in result["admission_detail"]


# --------------------------------------------------------------------------- #
# The sealed Work receipt
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        {"work_id": "fake"},
        {"work_id": "01a061a7-cea0-7503-b308-1f4029d450c8", "seal": "x" * 64},
        "01a061a7-cea0-7503-b308-1f4029d450c8",
        object(),
    ],
)
def test_only_a_minted_receipt_verifies(candidate):
    assert durable_admission.verify_work_receipt(candidate) is None


def test_a_handbuilt_receipt_object_has_no_valid_seal():
    """The dataclass is not the credential — the seal is, and its key never
    leaves this process."""
    forged = durable_admission.WorkReceipt(
        work_id="01a061a7-cea0-7503-b308-1f4029d450c8",
        idempotency_key="a" * 64,
        seam="execution",
        content_sha256="b" * 64,
        request_key="k8-forged",
        admitted_at=0,
        authority_pin_matched=True,
        seal="d" * 64,
    )

    assert durable_admission.verify_work_receipt(forged) is None


def test_k8_never_releases_a_work_receipt_capability():
    """Even the kernel's own ``effects_allowed`` does not buy one.

    A capability needs an AUTHENTICATED authority, and K8 only compares public
    pin fields. So the strongest outcome K8 can hold still yields no receipt.
    """
    outcome = durable_admission.AdmissionOutcome(
        state="admitted",
        reason_code="execution_seam_requested",
        seam="execution",
        work_id="01a061a7-cea0-7503-b308-1f4029d450c8",
        idempotency_key="a" * 64,
        content_sha256="b" * 64,
        request_key="k8-real",
        model_may_run=True,
        kernel_effects_allowed=True,
        authority_pin_matched=True,
    )

    assert outcome.authority_pin_matched is True
    assert outcome.authority_verified is False
    assert outcome.effects_allowed is False
    assert outcome.has_work_receipt is False
    assert outcome.work_receipt() is None


def test_authority_verified_is_read_only_and_always_false():
    """It is a property, so no construction path can assert verification."""
    outcome = durable_admission.AdmissionOutcome(
        state="admitted", reason_code="x", authority_pin_matched=True
    )

    assert outcome.authority_verified is False
    with pytest.raises((AttributeError, TypeError)):
        outcome.authority_verified = True  # type: ignore[misc]


def test_a_sealed_receipt_still_cannot_be_tampered_with():
    """The seal covers the fields, so an edited copy fails — proved on a
    receipt minted directly, since no admission path releases one."""
    import dataclasses

    real = durable_admission._mint_receipt(
        durable_admission._MINT,
        work_id="01a061a7-cea0-7503-b308-1f4029d450c8",
        idempotency_key="a" * 64,
        seam="execution",
        content_sha256="b" * 64,
        request_key="k8-real",
        admitted_at=0,
        authority_pin_matched=False,
    )
    assert durable_admission.verify_work_receipt(real) is real

    tampered = dataclasses.replace(real, work_id="somebody-elses-work")
    assert durable_admission.verify_work_receipt(tampered) is None


# --------------------------------------------------------------------------- #
# Carrying the gateway's admission into the turn
# --------------------------------------------------------------------------- #


@requires_k7
def test_gateway_pre_admission_is_consumed_once_and_not_readmitted(
    monkeypatch, tmp_path, turn_probe
):
    """The gateway admitted the user's own text. Re-admitting here would hash
    the model-enriched text (vision, STT, sender attribution) and mint a second
    Signal for one input."""
    _arm_real_kernel(monkeypatch, tmp_path)
    pre = durable_admission.admit_input(
        session_id="k8-session-alpha",
        request_text="what is in this picture",
        event_id="evt.telegram.9001",
    )
    durable_admission.publish_pre_admission(pre)
    spawned = []
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda *a, **kw: spawned.append(a) or (_ for _ in ()).throw(AssertionError()),
    )

    enriched = "what is in this picture\n\n[Image: a red bicycle]"
    with pytest.raises(ReachedTurnContext):
        conversation_loop.run_conversation(_agent(), enriched)

    assert turn_probe == [enriched]
    assert spawned == [], "the enriched text was admitted a second time"
    # Consumed, not left behind for the next turn on this context.
    assert durable_admission.consume_pre_admission() is None


@requires_k7
def test_a_blocked_pre_admission_stops_the_turn(monkeypatch, tmp_path, turn_probe):
    _arm_real_kernel(monkeypatch, tmp_path)
    blocked = durable_admission.admit_input(
        session_id="k8-session-alpha", request_text="hello", event_id=""
    )
    durable_admission.publish_pre_admission(blocked)

    result = conversation_loop.run_conversation(_agent(), "hello")

    assert turn_probe == []
    assert result["admission_blocked"] is True


@requires_k7
def test_subagent_turn_is_not_readmitted(monkeypatch, tmp_path, turn_probe):
    """The child runs inside the parent's already-admitted execution; admitting
    its turn again would open a second Work for one piece of work."""
    _arm_real_kernel(monkeypatch, tmp_path)
    spawned = []
    monkeypatch.setattr(
        durable_admission.subprocess,
        "run",
        lambda *a, **kw: spawned.append(a) or (_ for _ in ()).throw(AssertionError()),
    )

    child = _agent(session_id="k8-child-session", _delegate_depth=1)
    with pytest.raises(ReachedTurnContext):
        conversation_loop.run_conversation(child, "do the sub-task")

    assert turn_probe == ["do the sub-task"]
    assert spawned == []


@requires_k7
def test_an_admitted_turn_is_bound_for_inheritance(monkeypatch, tmp_path, turn_probe):
    """A read-only sync subtask inherits THIS turn rather than opening a Work."""
    _arm_real_kernel(monkeypatch, tmp_path)

    with pytest.raises(ReachedTurnContext):
        conversation_loop.run_conversation(_agent(), "look something up")

    inherited = durable_admission.current_admitted_turn()
    assert inherited is not None
    assert inherited.model_may_run is True
    # Inheriting a turn is not inheriting a Work.
    assert durable_admission.current_execution() is None
