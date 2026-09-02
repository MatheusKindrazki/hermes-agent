#!/usr/bin/env python3
"""K8 — durable admission in front of the model, and in front of every promise.

Two decisions, and they are not the same decision
-------------------------------------------------
1. **May the model run?** A natural input must be a durable Signal in the K7
   kernel before a compressor, a vision pass, an STT pass or a provider sees it.
   Losing an input because it was "just chat" is how a user's request disappears
   when a session dies.
2. **May an effect run?** Only a Work admitted by the *remote* authority
   releases one. A receipt minted by the offline test transport, a receipt
   relabelled by hand, a receipt for a different envelope and a receipt with no
   attestation at all are, all four, not authorisation.

Long work does not run here at all
----------------------------------
This is the correction that matters most. A background/long/promised execution
is NOT dispatched into the session-bound async registry, *even holding a valid
Work receipt*: that registry dies with the process that owns it, so an ACK from
it is a promise nobody can keep. Such a request returns an
:class:`ExecutionHandoff` — a stable, typed refusal that names the external Work
dispatcher — with **zero** local child, thread, DB row, or "dispatched/resume"
acknowledgement. Only a read-only subtask fully consumed inside the current
admitted turn may run locally.

The trust roots are frozen in code, not chosen by a caller
----------------------------------------------------------
``ADMITTER_SHA256`` and ``SCHEMA_SHA256`` are literals here. The environment
pins say *where* the kernel is; the constants say *which* kernel it must be, and
both files are hashed before anything is executed. A model argument, a tool
argument and user text can reach none of it: argv is a fixed list, there is no
shell, and the envelope rides stdin as one JSON document.

The outer budget is exactly 2.0s and no setting can raise it
------------------------------------------------------------
K7's client owns its own remote timeout and decides, inside that window, whether
the remote answered. ``TIMEOUT_SECONDS`` measures something different: how long
THIS process waits on the admitter subprocess. Expiring it proves nothing about
whether the kernel decided — the child may have died a millisecond before
writing its receipt — so it is ambiguous, fail-closed, non-retryable, and never
dispatched. Raising it would not make the answer safer, only the ambiguity
rarer, so it is a module constant with no config override on purpose.

Default is OFF, and OFF costs nothing
-------------------------------------
K8 arms only when ``HERMES_KERNEL_V1_MODE`` explicitly says so. Setting the
admitter path alone does NOT arm it. When off, :func:`admission_enabled` answers
from one environment read and returns: no config file is opened, no subprocess
exists, no database is opened or migrated, and no legacy notice changes.

B-prime assumptions carried from K7
-----------------------------------
* No kernel SQLite file is opened here; the CLI is the only door.
* Nothing renames or rewrites a live hierarchy.
* Integrity is not authority. ``decision_sha256`` is public data anybody can
  recompute, so it is never treated as a capability.
* Same-UID isolation is NOT promised. A process running as this user can reach
  the same state directory the kernel uses. This gate is about ordering,
  durability and honest refusal — not about defending against a peer holding
  our own credentials.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import platform as _platform
import re
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Mode — the first gate, and the cheapest one
# --------------------------------------------------------------------------- #
ENV_MODE = "HERMES_KERNEL_V1_MODE"
MODE_OFF = "off"
_ARMING_MODES = frozenset({"enforce", "on", "1", "true"})
_OFF_MODES = frozenset({"off", "0", "false", "disabled", ""})

# --------------------------------------------------------------------------- #
# Trust roots — the environment says WHERE, these constants say WHICH
# --------------------------------------------------------------------------- #
ENV_ADMITTER_BIN = "HERMES_KERNEL_ADMITTER_BIN"
ENV_SCHEMA_PATH = "HERMES_KERNEL_SCHEMA_PATH"
ENV_SCHEMA_SHA256 = "HERMES_KERNEL_SCHEMA_SHA256"

# The exact K7 build this consumer was approved against.
ADMITTER_SHA256 = "9110be8580acdaf0acee3b3fd0df149523b9f189c3a0c0898f5b90510c6c2a99"
SCHEMA_SHA256 = "92ce749bf6bfecf3528b83da9a5ef716f4ac6f7795342e2bf70e094eff306a3b"

# K7's own gate for its scripted offline transport. Forwarded only when the
# operator already set it. It grants nothing: K7 refuses the fixture without it,
# and anything minted through it speaks for `test_fixture`, which this module
# never accepts as authority for an effect.
ENV_K7_TEST_FIXTURE = "HERMES_KERNEL_TEST_FIXTURE"

# --------------------------------------------------------------------------- #
# The frozen slice of K7's contract this consumer pins
# --------------------------------------------------------------------------- #
KERNEL_VERSION = "hermes-kernel.v1"
ADMISSION_SCHEMA_VERSION = "hermes-kernel-admission.v1"
ENVELOPE_SCHEMA_VERSION = "work-envelope.v1"
ATTESTATION_ALG = "hmac-sha256"
ATTESTATION_VERSION = "hermes-kernel-admission-attestation.v1"
AUTHORITY_REMOTE = "remote"

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_CONTRACT = 2
EXIT_VERSION = 3
EXIT_INTERNAL = 4

SEAM_NONE = "none"
SEAM_EXECUTION = "execution"

STATE_ADMITTED = "admitted"
STATE_NO_WORK_REQUIRED = "no_work_required"
STATE_ADMISSION_PENDING = "admission_pending"
_SETTLED_STATES = (STATE_ADMITTED, STATE_NO_WORK_REQUIRED)

# Frozen. See the module docstring: no config key reads this.
TIMEOUT_SECONDS = 2.0

# The envelope's own cap. Above it the input cannot be represented, so it is
# refused by name rather than truncated into a different input.
MAX_INPUT_CHARS = 65536

# ``source_event.observed_at`` is the UPSTREAM observation time, and the schema
# admits 0. We use 0 as an explicit sentinel meaning "the producer did not give
# us one", because the alternative — stamping the wall clock — silently makes
# every envelope unique and turns each redelivery of one message into a second
# Signal. K7 compares the whole canonical envelope for a given idempotency key,
# so a clock in that field is a correctness bug, not a cosmetic one.
#
# The clock still exists, in the place that is honest about it: ``--now``, which
# K7 records as the Signal's ``recorded_at``. Observation time and recording
# time are separate facts and are kept apart on purpose — a replay legitimately
# has a new recording time and the SAME observation time.
#
# A nonzero value is passed only when a caller holds an immutable, deterministic
# upstream timestamp. Once a Signal exists under the sentinel it can never be
# "upgraded" to a real time: K7 answers ``signal_envelope_mismatch`` and the
# admission fails closed, which is the outcome we want — an input whose recorded
# identity quietly changed underneath the ledger is worse than a refusal.
OBSERVED_AT_UNKNOWN = 0

# ``surface`` names the PRODUCT that produced the envelope, not the chat
# platform the turn arrived on. Every Hermes-agent surface is ``hermes``; the
# enum's ``telegram`` belongs to the standalone Telegram producer.
ORIGIN_SURFACE = "hermes"

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_MACHINE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,47}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_REQUEST_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,119}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROOT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_KEY_ID_RE = re.compile(r"^[0-9a-f]{16}$")

CONFIG_SECTION = ("agent", "durable_admission")

# The external dispatcher a long/promised execution is handed off to. Named as
# data so the refusal is stable and the operator sees one string everywhere.
EXTERNAL_WORK_DISPATCHER = "hermes-work-control"


class _State(threading.local):
    outcome: Optional["AdmissionOutcome"] = None


_state = _State()

# The verified pre-admission produced at the gateway ingress and consumed once,
# further in. A ContextVar (not an argument) because the real gateway paths
# reach ``run_conversation`` through several call shapes, and threading a new
# parameter through them would touch files outside this package's scope.
_PRE_ADMISSION: contextvars.ContextVar[Optional["AdmissionOutcome"]] = (
    contextvars.ContextVar("hermes_k8_pre_admission", default=None)
)
# The admitted execution the current call tree runs inside. A nested/sync
# delegation inherits it instead of opening a second Work for one execution.
_EXECUTION: contextvars.ContextVar[Optional["WorkReceipt"]] = contextvars.ContextVar(
    "hermes_k8_execution", default=None
)
# The admitted turn this call tree runs inside. A read-only sync subtask
# inherits it; it is not a Work, and it authorises nothing detached.
_ADMITTED_TURN: contextvars.ContextVar[Optional["AdmissionOutcome"]] = (
    contextvars.ContextVar("hermes_k8_admitted_turn", default=None)
)

# Process-local seal. It makes a receipt unforgeable *by shape*: a hand-built
# dict or a hand-built dataclass cannot carry a valid seal because the key never
# leaves this process. It is NOT an authority claim — that is the attestation,
# and it is checked separately.
_SEAL_KEY = secrets.token_bytes(32)
_MINT = object()


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class WorkReceipt:
    """A Work this process admitted. Only :func:`_mint_receipt` produces one."""

    work_id: str
    idempotency_key: str
    seam: str
    content_sha256: str
    request_key: str
    admitted_at: int
    authority_verified: bool
    seal: str = field(repr=False)

    def sealed_core(self) -> Dict[str, Any]:
        return {
            "work_id": self.work_id,
            "idempotency_key": self.idempotency_key,
            "seam": self.seam,
            "content_sha256": self.content_sha256,
            "request_key": self.request_key,
            "admitted_at": self.admitted_at,
            "authority_verified": self.authority_verified,
        }

    def as_dict(self) -> Dict[str, Any]:
        """Plain data for logs/persistence. Carries no seal, so it is inert."""
        return dict(self.sealed_core())


def _seal_for(core: Mapping[str, Any]) -> str:
    return hmac.new(_SEAL_KEY, _canonical(dict(core)).encode("utf-8"), hashlib.sha256).hexdigest()


def _mint_receipt(_token: object, **core: Any) -> WorkReceipt:
    if _token is not _MINT:  # pragma: no cover - defensive
        raise RuntimeError("WorkReceipt may only be minted by durable admission")
    return WorkReceipt(seal=_seal_for(core), **core)


def verify_work_receipt(candidate: Any) -> Optional[WorkReceipt]:
    """Return the receipt only if this process minted it, else ``None``.

    A dict is never a receipt, however complete it looks: ``{"work_id": "fake"}``
    and a hand-assembled ``WorkReceipt`` both fail here, because the seal is an
    HMAC under a key that never leaves this process.
    """
    if not isinstance(candidate, WorkReceipt):
        return None
    expected = _seal_for(candidate.sealed_core())
    if not hmac.compare_digest(expected, candidate.seal or ""):
        return None
    if not (isinstance(candidate.work_id, str) and candidate.work_id.strip()):
        return None
    return candidate


@dataclass(frozen=True)
class ExecutionHandoff:
    """A long/promised execution that this process refuses to run locally."""

    reason_code: str
    detail: str
    dispatcher: str = EXTERNAL_WORK_DISPATCHER
    work_id: Optional[str] = None
    request_key: Optional[str] = None
    retryable: bool = False

    def payload(self) -> Dict[str, Any]:
        """The stable, typed shape a tool returns. Never says 'dispatched'."""
        return {
            "status": "handoff_required",
            "handoff": {
                "dispatcher": self.dispatcher,
                "reason": self.reason_code,
                "work_id": self.work_id,
                "request_key": self.request_key,
            },
            "retryable": self.retryable,
            "error": self.detail,
        }


@dataclass(frozen=True)
class AdmissionOutcome:
    """One answer about one input. Absence of permission is never permission."""

    state: str
    reason_code: str
    detail: str = ""
    seam: str = SEAM_NONE
    work_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    content_sha256: Optional[str] = None
    request_key: Optional[str] = None
    event_id: Optional[str] = None
    session_id: Optional[str] = None
    model_may_run: bool = False
    kernel_effects_allowed: bool = False
    authority_verified: bool = False
    signal_persisted: bool = False
    replayed: bool = False
    retryable: bool = False
    mode_off: bool = False
    admitted_at: int = 0
    receipt: Optional[Dict[str, Any]] = field(default=None, repr=False)

    @property
    def blocked(self) -> bool:
        return not self.mode_off and not self.model_may_run

    @property
    def effects_allowed(self) -> bool:
        """An effect needs BOTH: the kernel released it AND the authority that
        released it was verified as the pinned remote. A fixture-minted or
        unattested receipt is refused here, not downstream."""
        return bool(self.kernel_effects_allowed and self.authority_verified)

    @property
    def has_work_receipt(self) -> bool:
        return bool(
            self.state == STATE_ADMITTED
            and self.effects_allowed
            and isinstance(self.work_id, str)
            and self.work_id.strip()
        )

    def work_receipt(self) -> Optional[WorkReceipt]:
        if not self.has_work_receipt:
            return None
        return _mint_receipt(
            _MINT,
            work_id=self.work_id,
            idempotency_key=self.idempotency_key or "",
            seam=self.seam,
            content_sha256=self.content_sha256 or "",
            request_key=self.request_key or "",
            admitted_at=self.admitted_at,
            authority_verified=self.authority_verified,
        )


def _off() -> AdmissionOutcome:
    """The pre-K8 world: no gate, no claim, nothing recorded."""
    return AdmissionOutcome(
        state="mode_off",
        reason_code="durable_admission_disabled",
        model_may_run=True,
        mode_off=True,
    )


def _refuse(
    reason_code: str,
    detail: str = "",
    *,
    seam: str = SEAM_NONE,
    retryable: bool = False,
    state: str = STATE_ADMISSION_PENDING,
    signal_persisted: bool = False,
    **extra: Any,
) -> AdmissionOutcome:
    return AdmissionOutcome(
        state=state,
        reason_code=reason_code,
        detail=detail,
        seam=seam,
        model_may_run=False,
        kernel_effects_allowed=False,
        authority_verified=False,
        signal_persisted=signal_persisted,
        retryable=retryable,
        **extra,
    )


# --------------------------------------------------------------------------- #
# The mode gate — one environment read, no side effects of any kind
# --------------------------------------------------------------------------- #


def admission_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Is K8 armed?

    Arming is explicit: ``HERMES_KERNEL_V1_MODE`` must say so. Pointing
    ``HERMES_KERNEL_ADMITTER_BIN`` at a kernel does NOT arm anything — a path is
    where the kernel lives, not a decision to enforce with it.

    When this answers False it has opened no config file, spawned no process,
    touched no database and changed no notice.
    """
    env = os.environ if environ is None else environ
    mode = (env.get(ENV_MODE) or "").strip().lower()
    if mode in _ARMING_MODES:
        return True
    if mode not in _OFF_MODES:
        # An unrecognised mode is not an invitation to guess which way the
        # operator meant it; the safe reading of a typo is "not armed".
        logger.warning("%s=%r is not a recognised mode; K8 stays off", ENV_MODE, mode)
    return False


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def last_outcome() -> Optional[AdmissionOutcome]:
    return getattr(_state, "outcome", None)


def reset_state_for_tests() -> None:
    _state.outcome = None
    _PRE_ADMISSION.set(None)
    _EXECUTION.set(None)
    _ADMITTED_TURN.set(None)


def _remember(outcome: AdmissionOutcome) -> AdmissionOutcome:
    _state.outcome = outcome
    return outcome


# --------------------------------------------------------------------------- #
# Request-scoped carriers
# --------------------------------------------------------------------------- #


def publish_pre_admission(outcome: AdmissionOutcome) -> contextvars.Token:
    """Publish the gateway's verified admission for this request."""
    return _PRE_ADMISSION.set(outcome)


def consume_pre_admission() -> Optional[AdmissionOutcome]:
    """Take the pre-admission exactly once.

    Consuming (rather than peeking) is what stops a later turn on the same
    context from riding an earlier turn's admission.
    """
    outcome = _PRE_ADMISSION.get()
    if outcome is not None:
        _PRE_ADMISSION.set(None)
    return outcome


def current_execution() -> Optional[WorkReceipt]:
    return verify_work_receipt(_EXECUTION.get())


def bind_admitted_turn(outcome: Optional[AdmissionOutcome]) -> None:
    """Mark the turn this call tree is running inside as admitted.

    Bound once per turn, immediately after admission and before any tool can
    run. A read-only sync subtask inherits it instead of opening its own Work —
    it is consumed entirely by this turn, so it IS this turn's execution.

    Rebinding every turn is what keeps it honest: a blocked turn returns before any
    tool runs, so a previous turn's value can never release one.
    """
    _ADMITTED_TURN.set(outcome)


def current_admitted_turn() -> Optional[AdmissionOutcome]:
    outcome = _ADMITTED_TURN.get()
    if outcome is None or outcome.mode_off or not outcome.model_may_run:
        return None
    return outcome


@contextmanager
def execution_scope(receipt: Optional[WorkReceipt]):
    """Bind the admitted execution for everything this call tree runs."""
    token = _EXECUTION.set(receipt)
    try:
        yield receipt
    finally:
        _EXECUTION.reset(token)


# --------------------------------------------------------------------------- #
# Settings — behavioural config only, and only once armed
# --------------------------------------------------------------------------- #


def _settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        node: Any = load_config_readonly()
        for key in CONFIG_SECTION:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        return dict(node) if isinstance(node, dict) else {}
    except Exception:
        logger.debug("durable admission config read failed", exc_info=True)
        return {}


def _config_disarms(settings: Mapping[str, Any]) -> bool:
    return str(settings.get("mode", "")).strip().lower() in {"off", "0", "false", "disabled"}


def _state_dir(settings: Mapping[str, Any]) -> str:
    configured = str(settings.get("state_dir") or "").strip()
    if configured:
        return configured
    from hermes_constants import get_hermes_home

    return str(get_hermes_home() / "kernel")


# --------------------------------------------------------------------------- #
# Trust-root resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _TrustRoots:
    admitter_bin: str
    schema_path: str
    root_dir: str


class _TrustRootError(RuntimeError):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__("%s: %s" % (reason_code, detail))
        self.reason_code = reason_code
        self.detail = detail


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_trust_roots(environ: Optional[Mapping[str, str]] = None) -> _TrustRoots:
    env = os.environ if environ is None else environ

    raw_bin = (env.get(ENV_ADMITTER_BIN) or "").strip()
    bin_path = Path(raw_bin)
    if not raw_bin or not bin_path.is_absolute():
        raise _TrustRootError(
            "admitter_bin_invalid",
            "%s must be an absolute path, got %r" % (ENV_ADMITTER_BIN, raw_bin),
        )
    if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
        raise _TrustRootError(
            "admitter_bin_invalid",
            "%s is not an executable file: %s" % (ENV_ADMITTER_BIN, bin_path),
        )
    actual_bin = _sha256_file(bin_path)
    if actual_bin != ADMITTER_SHA256:
        # A different kernel binary is a different kernel, whatever the path
        # says. Refuse before executing it, not after reading its answer.
        raise _TrustRootError(
            "admitter_hash_mismatch",
            "%s hashes to %s; this consumer is pinned to %s"
            % (bin_path, actual_bin, ADMITTER_SHA256),
        )

    declared = (env.get(ENV_SCHEMA_SHA256) or "").strip().lower()
    if not _SHA256_RE.match(declared):
        raise _TrustRootError(
            "schema_pin_invalid", "%s must be 64 hex chars" % ENV_SCHEMA_SHA256
        )
    if declared != SCHEMA_SHA256:
        # The environment may say WHERE the contract is; it may not say which
        # contract this consumer agreed to.
        raise _TrustRootError(
            "schema_pin_not_frozen",
            "%s declares %s; this consumer is pinned to %s"
            % (ENV_SCHEMA_SHA256, declared, SCHEMA_SHA256),
        )

    raw_schema = (env.get(ENV_SCHEMA_PATH) or "").strip()
    schema_path = Path(raw_schema)
    if not raw_schema or not schema_path.is_absolute() or not schema_path.is_file():
        raise _TrustRootError(
            "schema_path_invalid",
            "%s must point at an existing absolute file" % ENV_SCHEMA_PATH,
        )
    actual_schema = _sha256_file(schema_path)
    if actual_schema != SCHEMA_SHA256:
        raise _TrustRootError(
            "schema_hash_mismatch",
            "%s hashes to %s, pinned %s" % (schema_path, actual_schema, SCHEMA_SHA256),
        )

    return _TrustRoots(str(bin_path), str(schema_path), str(bin_path.resolve().parents[2]))


# --------------------------------------------------------------------------- #
# Authority — the pin comparison K8 owns
# --------------------------------------------------------------------------- #


def _authority_pin(settings: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """The pinned remote authority, or None when nothing is pinned.

    Mirrors K7's ``AuthorityPin``: which authority (``root_id``), which key of
    it (``key_id``), and where the verifier — not the caller — fetches the
    credential. With no pin configured there is no authority to compare against,
    and no receipt may authorise an effect.
    """
    raw = settings.get("authority_pin")
    if not isinstance(raw, Mapping):
        return None
    source = str(raw.get("authority_source") or "").strip()
    root_id = str(raw.get("authority_root_id") or "").strip().lower()
    key_id = str(raw.get("attestation_key_id") or "").strip().lower()
    secret_ref = str(raw.get("secret_ref") or "").strip()
    if source != AUTHORITY_REMOTE:
        # Pinning the offline test domain as an authority would make the test
        # double a bypass, which is the defect the domain separation exists for.
        logger.error("authority_pin.authority_source must be %r", AUTHORITY_REMOTE)
        return None
    if not (_ROOT_ID_RE.match(root_id) and _KEY_ID_RE.match(key_id) and secret_ref):
        logger.error("authority_pin is incomplete; no receipt can be authorised")
        return None
    return {
        "authority_source": source,
        "authority_root_id": root_id,
        "attestation_key_id": key_id,
        "secret_ref": secret_ref,
    }


def _authority_verified(document: Mapping[str, Any], pin: Optional[Mapping[str, str]]) -> bool:
    """Compare everything a forger controls against the pin, in K7's order.

    ⚠️ This is the pin-comparison half of K7's ``verify_pinned_attestation``.
    The remaining half — recomputing the HMAC — needs the credential behind
    ``secret_ref``, which this package is not permitted to hold. So a receipt
    that passes here is *not yet proven authentic*; it has only stopped being
    obviously forged. Nothing in K8 runs an effect on the strength of it: a
    long execution is handed off, never executed locally. Closing the HMAC half
    is K9's, and is stated in ``EXPORTED_CONTRACT``.
    """
    if not pin:
        return False
    if document.get("authority_source") != pin["authority_source"]:
        return False
    if str(document.get("authority_root_id") or "").lower() != pin["authority_root_id"]:
        return False
    attestation = document.get("attestation")
    if not isinstance(attestation, Mapping):
        # An unattested receipt is text. Absence never becomes permission.
        return False
    if attestation.get("alg") != ATTESTATION_ALG:
        return False
    if attestation.get("version") != ATTESTATION_VERSION:
        return False
    if attestation.get("authority_source") != pin["authority_source"]:
        return False
    if str(attestation.get("authority_root_id") or "").lower() != pin["authority_root_id"]:
        return False
    if str(attestation.get("key_id") or "").lower() != pin["attestation_key_id"]:
        return False
    value = attestation.get("value")
    return isinstance(value, str) and len(value) == 64


# --------------------------------------------------------------------------- #
# Envelope construction
# --------------------------------------------------------------------------- #


class _EnvelopeError(RuntimeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _sanitize_machine(raw: str) -> str:
    lowered = (raw or "").strip().lower()
    reduced = re.sub(r"[^a-z0-9._-]", "-", lowered).strip("-")[:64]
    return reduced if _MACHINE_RE.match(reduced or "") else ""


def _active_profile() -> str:
    from hermes_constants import get_hermes_home

    try:
        home = get_hermes_home()
    except Exception:
        return "default"
    if home.parent.name == "profiles":
        candidate = home.name.strip().lower()
        return candidate if _PROFILE_RE.match(candidate) else "default"
    return "default"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def build_envelope(
    *,
    session_id: str,
    request_text: str,
    seam: str,
    settings: Mapping[str, Any],
    event_id: str,
    event_kind: str,
    declares_execution: bool,
    observed_at: int = OBSERVED_AT_UNKNOWN,
) -> Dict[str, Any]:
    """Build one closed envelope, or refuse to build one at all.

    An identity that cannot be represented is a refusal, never a rewrite:
    silently normalising a session id would merge two conversations onto one
    Signal, which is the leak the closed schema exists to prevent.
    """
    text = request_text if isinstance(request_text, str) else ""
    if not text.strip():
        raise _EnvelopeError("input.text is empty; there is nothing to admit")
    if len(text) > MAX_INPUT_CHARS:
        raise _EnvelopeError(
            "input_too_large: %d chars exceeds the frozen envelope cap of %d"
            % (len(text), MAX_INPUT_CHARS)
        )

    sid = (session_id or "").strip()
    if not _SESSION_ID_RE.match(sid):
        raise _EnvelopeError("origin.session_id %r is not representable" % sid)

    resolved_event = (event_id or "").strip()
    if not _EVENT_ID_RE.match(resolved_event):
        # Armed, with no stable platform identity, the only honest answer is to
        # stop: a minted id would make a replay look like a second input.
        raise _EnvelopeError("source_event.event_id %r is not a stable identity" % resolved_event)

    machine = _sanitize_machine(str(settings.get("machine") or "") or _platform.node() or "")
    if not machine:
        raise _EnvelopeError("origin.machine could not be resolved")

    tenant = str(settings.get("tenant") or "default").strip().lower()
    if not _TENANT_RE.match(tenant):
        raise _EnvelopeError("tenant %r is not representable" % tenant)

    if not isinstance(observed_at, int) or isinstance(observed_at, bool) or observed_at < 0:
        raise _EnvelopeError(
            "source_event.observed_at %r is not a non-negative integer" % (observed_at,)
        )

    profile = _active_profile()
    digest = content_sha256(text)
    request_key = "k8-%s" % _digest(sid, resolved_event, digest, seam)[:40]
    if not _REQUEST_KEY_RE.match(request_key):  # pragma: no cover - hex is in range
        raise _EnvelopeError("binding.request_key %r is not representable" % request_key)

    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "origin": {"surface": ORIGIN_SURFACE, "session_id": sid, "machine": machine},
        "tenant": tenant,
        "profile": profile,
        "source_event": {
            "event_id": resolved_event,
            "kind": event_kind,
            # Sentinel unless the caller held a deterministic upstream time.
            # NEVER the wall clock: see OBSERVED_AT_UNKNOWN.
            "observed_at": int(observed_at),
        },
        "input": {"text": text, "declares_execution": bool(declares_execution)},
        "content_sha256": digest,
        "binding": {"request_key": request_key},
    }


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #

# Forwarded by name. PYTHONPATH is deliberately absent: a caller-set one could
# shadow ``control.kernel`` inside the admitter, letting the thing being gated
# supply its own gate.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT")


def _child_env(settings: Mapping[str, Any]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for name in _ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.setdefault("PATH", "/usr/bin:/bin")
    fixture_gate = os.environ.get(ENV_K7_TEST_FIXTURE)
    if fixture_gate:
        env[ENV_K7_TEST_FIXTURE] = fixture_gate
    secret_ref = str(settings.get("secret_ref") or "")
    if secret_ref.startswith("env://"):
        name = secret_ref[len("env://") :].strip()
        value = os.environ.get(name)
        if name and value:
            env[name] = value
    return env


def _argv(roots: _TrustRoots, settings: Mapping[str, Any], seam: str, now: int) -> list:
    """A fixed argv list. No shell, no interpolation, no caller-shaped flags."""
    argv = [
        roots.admitter_bin,
        "admit",
        "--stdin",
        "--now",
        str(int(now)),
        "--seam",
        seam,
        "--state-dir",
        _state_dir(settings),
        "--require-schema-sha256",
        SCHEMA_SHA256,
    ]
    fixture = str(settings.get("transport_fixture") or "").strip()
    base_url = str(settings.get("jarvis_base_url") or "").strip()
    secret_ref = str(settings.get("secret_ref") or "").strip()
    if fixture:
        argv += ["--transport-fixture", fixture]
    if base_url:
        argv += ["--jarvis-base-url", base_url]
    if secret_ref:
        argv += ["--secret-ref", secret_ref]
    return argv


def _verify_receipt(
    document: Any, *, envelope: Mapping[str, Any], seam: str, exit_code: int
) -> Optional[str]:
    """Return a refusal code, or None when the receipt may be read.

    Checks the pinned contract, the echo of the envelope we sent, and agreement
    between the exit code and the state. It does NOT recompute
    ``decision_sha256``: that hash is public data, so gating on it would be
    gating on nothing.
    """
    if not isinstance(document, dict):
        return "receipt_unreadable"
    if document.get("kernel_version") != KERNEL_VERSION:
        return "kernel_version_mismatch"
    if document.get("schema_version") != ADMISSION_SCHEMA_VERSION:
        return "kernel_version_mismatch"
    if document.get("envelope_schema_version") not in (None, ENVELOPE_SCHEMA_VERSION):
        return "kernel_version_mismatch"
    if document.get("schema_sha256") != SCHEMA_SHA256:
        return "schema_hash_mismatch"

    state = document.get("state")
    if state not in (STATE_ADMITTED, STATE_NO_WORK_REQUIRED, STATE_ADMISSION_PENDING):
        return "receipt_incoherent"
    if state in _SETTLED_STATES and exit_code != EXIT_OK:
        return "receipt_incoherent"
    if state == STATE_ADMISSION_PENDING and exit_code != EXIT_PENDING:
        return "receipt_incoherent"

    origin = envelope["origin"]
    event = envelope["source_event"]
    for key, expected in (
        ("content_sha256", envelope["content_sha256"]),
        ("tenant", envelope["tenant"]),
        ("profile", envelope["profile"]),
        ("origin_surface", origin["surface"]),
        ("origin_session_id", origin["session_id"]),
        ("origin_machine", origin["machine"]),
        ("source_event_kind", event["kind"]),
        ("source_event_id", event["event_id"]),
        ("seam", seam),
    ):
        if document.get(key) != expected:
            return "receipt_envelope_mismatch"
    # The binding is what stops a receipt admitted for one request releasing
    # another, so a missing one is a mismatch, not a tolerated absence.
    if document.get("binding") != envelope.get("binding"):
        return "receipt_envelope_mismatch"
    if not isinstance(document.get("idempotency_key"), str):
        return "receipt_incoherent"
    return None


def _run_admission(
    *,
    session_id: str,
    request_text: str,
    seam: str,
    event_id: str,
    event_kind: str,
    declares_execution: bool,
    observed_at: int = OBSERVED_AT_UNKNOWN,
) -> AdmissionOutcome:
    if not admission_enabled():
        return _off()

    settings = _settings()
    if _config_disarms(settings):
        return _off()

    try:
        roots = _resolve_trust_roots()
    except _TrustRootError as exc:
        logger.error("durable admission trust root refused: %s", exc)
        return _remember(_refuse(exc.reason_code, exc.detail, seam=seam, session_id=session_id))

    try:
        envelope = build_envelope(
            session_id=session_id,
            request_text=request_text,
            seam=seam,
            settings=settings,
            event_id=event_id,
            event_kind=event_kind,
            declares_execution=declares_execution,
            observed_at=observed_at,
        )
    except _EnvelopeError as exc:
        return _remember(
            _refuse("envelope_invalid", exc.detail, seam=seam, session_id=session_id)
        )

    # The recording clock, deliberately NOT the envelope's observation time.
    # K7 stores it as the Signal's ``recorded_at``; it varies per attempt and
    # does not enter the canonical envelope, so a replay stays byte-identical.
    now = int(time.time())
    common = {
        "seam": seam,
        "content_sha256": envelope["content_sha256"],
        "request_key": envelope["binding"]["request_key"],
        "event_id": envelope["source_event"]["event_id"],
        "session_id": session_id,
    }

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv list, shell=False
            _argv(roots, settings, seam, now),
            input=json.dumps(envelope, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=roots.root_dir,
            env=_child_env(settings),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "durable admission expired the %.1fs outer budget (ambiguous, "
            "fail-closed): session=%s seam=%s",
            TIMEOUT_SECONDS,
            session_id,
            seam,
        )
        return _remember(
            _refuse(
                "admitter_timeout",
                "the admitter did not answer within the frozen outer budget; the "
                "outcome is ambiguous and must not be re-asked from here",
                retryable=False,
                **common,
            )
        )
    except OSError as exc:
        return _remember(
            _refuse(
                "admitter_unreachable",
                "%s: %s" % (type(exc).__name__, exc),
                retryable=True,
                **common,
            )
        )

    try:
        document = json.loads(completed.stdout or "")
    except (TypeError, ValueError):
        document = None
    if document is None:
        return _remember(
            _refuse("receipt_unreadable", (completed.stderr or "").strip()[:400], **common)
        )

    by_exit = {
        EXIT_INTERNAL: "admitter_internal_failure",
        EXIT_VERSION: "schema_hash_mismatch",
        EXIT_CONTRACT: "envelope_invalid",
    }
    if completed.returncode in by_exit:
        reason = by_exit[completed.returncode]
        if document.get("error_code") == "signal_envelope_mismatch":
            # This event id already has a Signal recorded under DIFFERENT
            # canonical bytes — typically an attempt to re-admit it with a real
            # observation time after it was first recorded under the sentinel.
            # It fails closed by name: silently upgrading a recorded identity is
            # exactly the rewrite the ledger exists to prevent.
            reason = "signal_envelope_mismatch"
        return _remember(
            _refuse(
                reason,
                str(document.get("detail") or document.get("error_code") or "")[:400],
                **common,
            )
        )

    refusal = _verify_receipt(
        document, envelope=envelope, seam=seam, exit_code=completed.returncode
    )
    if refusal is not None:
        logger.error("durable admission receipt refused (%s)", refusal)
        return _remember(
            _refuse(refusal, str(document.get("detail") or "")[:400], **common)
        )

    signal_persisted = bool(document.get("signal_persisted_before_model")) and isinstance(
        document.get("signal"), dict
    )
    state = document["state"]
    if state == STATE_ADMISSION_PENDING:
        reason = str(document.get("reason_code") or "admission_pending")
        return _remember(
            _refuse(
                reason,
                str(document.get("detail") or "")[:400],
                retryable=reason == "jarvis_unavailable",
                signal_persisted=signal_persisted,
                idempotency_key=document.get("idempotency_key"),
                receipt=document,
                **common,
            )
        )

    work_id = document.get("work_id")
    return _remember(
        AdmissionOutcome(
            state=state,
            reason_code=str(document.get("reason_code") or ""),
            detail=str(document.get("detail") or ""),
            work_id=work_id if isinstance(work_id, str) and work_id.strip() else None,
            idempotency_key=document.get("idempotency_key"),
            model_may_run=bool(document.get("model_may_run")),
            kernel_effects_allowed=bool(document.get("effects_allowed")),
            authority_verified=_authority_verified(document, _authority_pin(settings)),
            signal_persisted=signal_persisted,
            replayed=bool(document.get("replayed")),
            admitted_at=int(now),
            receipt=document,
            **common,
        )
    )


# --------------------------------------------------------------------------- #
# Public seams
# --------------------------------------------------------------------------- #


def admit_input(
    *,
    session_id: str,
    request_text: str,
    event_id: str,
    declares_execution: bool = False,
    observed_at: int = OBSERVED_AT_UNKNOWN,
) -> AdmissionOutcome:
    """Admit one natural input, before anything reads or rewrites it.

    ``observed_at`` defaults to the sentinel. Pass a real value ONLY when the
    producer holds an immutable, deterministic upstream timestamp for this
    exact event — one that will be identical on a redelivery. Anything derived
    from the current time is not that, and would fork the Signal.
    """
    return _run_admission(
        session_id=session_id,
        request_text=request_text,
        seam=SEAM_EXECUTION if declares_execution else SEAM_NONE,
        event_id=event_id,
        event_kind="chat_message",
        declares_execution=declares_execution,
        observed_at=observed_at,
    )


def admit_execution(
    *,
    session_id: str,
    request: Mapping[str, Any],
    event_id: str,
    observed_at: int = OBSERVED_AT_UNKNOWN,
) -> AdmissionOutcome:
    """Admit one execution request.

    ``request`` is canonicalised whole, so the same goal with a different
    context, role or output schema is a DIFFERENT execution and cannot collapse
    onto the first one's Work.
    """
    return _run_admission(
        session_id=session_id,
        request_text=_canonical(request),
        seam=SEAM_EXECUTION,
        event_id=event_id,
        event_kind="system_event",
        declares_execution=True,
        observed_at=observed_at,
    )


def execution_event_id(session_id: str, request: Mapping[str, Any]) -> str:
    """A stable identity for one execution request within one session."""
    return "exec.%s" % _digest(session_id or "", _canonical(request))[:48]


def platform_event_id(surface: str, raw_identity: Any) -> Optional[str]:
    """Normalise a platform's own message identity into an event id.

    Returns None when the platform gave us nothing stable — the caller fails
    closed rather than inventing one, because a minted id turns a replay of the
    same message into a second Signal.
    """
    token = str(raw_identity if raw_identity is not None else "").strip()
    if not token:
        return None
    reduced = re.sub(r"[^A-Za-z0-9._:-]", "-", token)[:96]
    candidate = "evt.%s.%s" % (re.sub(r"[^a-z0-9]", "", (surface or "x").lower())[:16] or "x", reduced)
    return candidate if _EVENT_ID_RE.match(candidate) else None


def handoff_for(outcome: AdmissionOutcome, *, detail: str) -> ExecutionHandoff:
    return ExecutionHandoff(
        reason_code=outcome.reason_code or "work_required",
        detail=detail,
        work_id=outcome.work_id,
        request_key=outcome.request_key,
        retryable=outcome.retryable,
    )


def _turn_text(user_message: Any) -> str:
    """Flatten a provider-shaped message into the text that was actually said."""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = [
            str(block.get("text") or "")
            for block in user_message
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    if isinstance(user_message, dict):
        return str(user_message.get("text") or user_message.get("content") or "")
    return ""


def blocked_turn_result(
    agent: Any, outcome: AdmissionOutcome, conversation_history: Any
) -> Dict[str, Any]:
    """The terminal turn result for a refused admission.

    ``failed`` stays False and ``retryable`` says whether asking again is legal,
    mirroring the compression-defer shape: this is a turn that did not run, not
    a turn that broke. Neither branch ever claims work was dispatched.
    """
    if outcome.signal_persisted:
        final = (
            "Your message was durably recorded, but it has not been admitted "
            "for execution yet, so nothing has run. It stays pending until the "
            "work ledger confirms it."
        )
    else:
        final = (
            "Your message was not accepted for execution and nothing has run. "
            "The work ledger could not confirm it, so it was not started."
        )
    return {
        "final_response": final,
        "messages": list(conversation_history) if isinstance(conversation_history, list) else [],
        "api_calls": 0,
        "completed": False,
        "failed": False,
        "partial": True,
        "error": final,
        "retryable": outcome.retryable,
        "admission_blocked": True,
        "admission_state": outcome.state,
        "admission_reason_code": outcome.reason_code,
        "admission_detail": outcome.detail,
        "admission_signal_persisted": outcome.signal_persisted,
        "session_id": getattr(agent, "session_id", None),
    }


def admit_turn_or_block(
    agent: Any, user_message: Any, *, conversation_history: Any = None
) -> Optional[Dict[str, Any]]:
    """The conversation seam. Returns None to proceed, or a terminal result.

    Placed after the MoA decode (so what is admitted is the text the user
    actually wrote) and before ``build_turn_context`` (which runs preflight
    compression, the oversized-resume rebuild and the ``pre_llm_call`` hook).
    The Codex app-server runtime branches after that call, so it is covered here
    with no separate wiring.

    When the gateway already admitted this request it published a verified
    outcome; this consumes it once and does NOT re-admit — re-admitting would
    hash the model-enriched text (vision descriptions, STT, sender attribution)
    and mint a second Signal for one input.

    A delegated child is skipped: it runs inside the parent's already-admitted
    execution, and admitting its turn would open a second Work for one job.
    """
    if not admission_enabled():
        return None
    if getattr(agent, "_delegate_depth", 0) or getattr(agent, "_subagent_id", None):
        return None

    pre = consume_pre_admission()
    if pre is not None:
        if pre.mode_off or pre.model_may_run:
            # Bound before any tool can run, so a read-only synchronous subtask
            # inherits THIS turn's admission instead of opening its own Work.
            bind_admitted_turn(pre)
            return None
        bind_admitted_turn(None)
        return blocked_turn_result(agent, pre, conversation_history)

    # No gateway pre-admission: a CLI/TUI/cron/API turn. The text here is still
    # the user's own, so this is the first and only admission for it.
    session_id = str(getattr(agent, "session_id", "") or "")
    text = _turn_text(user_message)
    outcome = admit_input(
        session_id=session_id,
        request_text=text,
        event_id="turn.%s" % _digest(session_id, content_sha256(text))[:48],
    )
    if outcome.mode_off or outcome.model_may_run:
        bind_admitted_turn(outcome)
        return None
    bind_admitted_turn(None)
    return blocked_turn_result(agent, outcome, conversation_history)


# --------------------------------------------------------------------------- #
# What K9 is handed
# --------------------------------------------------------------------------- #
EXPORTED_CONTRACT: Dict[str, Any] = {
    "package": "K8",
    "kernel_version": KERNEL_VERSION,
    "admission_schema_version": ADMISSION_SCHEMA_VERSION,
    "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
    "admitter_sha256": ADMITTER_SHA256,
    "schema_sha256": SCHEMA_SHA256,
    "outer_timeout_seconds": TIMEOUT_SECONDS,
    "arming": "%s in %s" % (ENV_MODE, sorted(_ARMING_MODES)),
    "seams": {
        "gateway_ingress": "gateway.run.GatewayRunner._handle_message (before "
        "commands, interrupt/STT, compression and vision)",
        "gateway_background_task": "gateway.run._run_background_task_inner "
        "(before A2A image preprocessing)",
        "turn": "agent.conversation_loop.run_conversation (after the MoA decode, "
        "before build_turn_context); consumes the gateway pre-admission once",
        "execution": "tools.delegate_tool.delegate_task",
    },
    "long_execution": "handed off to %s; never dispatched into the session-bound "
    "async registry" % EXTERNAL_WORK_DISPATCHER,
    "observed_at": "0 is the explicit 'upstream observation time unknown' "
    "sentinel; the wall clock is NEVER used in the envelope. Recording time "
    "rides --now and is stored by K7 as the Signal's recorded_at.",
    "still_open_for_k9": (
        "recompute the attestation HMAC against the pinned AuthorityPin and the "
        "credential behind its secret_ref; K8 performs the pin comparison half "
        "(source, root, key id, alg, version, binding) and treats a receipt that "
        "passes it as NOT-obviously-forged, never as proven authentic",
        "effects_allowed enforcement for mutating tools other than delegate_task "
        "(needs model_tools/tool_executor, outside K8's file scope)",
        "an input above MAX_INPUT_CHARS cannot be represented in the frozen "
        "envelope and is refused as input_too_large",
        "plumbing a real per-platform observation timestamp: every surface "
        "currently admits under OBSERVED_AT_UNKNOWN, because a deterministic "
        "upstream time would have to come from the ~20 platform adapters and "
        "an event first recorded under the sentinel can never be upgraded",
    ),
}
