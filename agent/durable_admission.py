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
import stat
import tempfile
import uuid
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

# Non-authoritative provenance: sealed against accidental reconstruction or
# mutation in this process, not a security boundary against same-UID code.
_ORIGIN_KEY = secrets.token_bytes(32)
_EFFECT_ORIGIN = contextvars.ContextVar("hermes_effect_origin", default=None)


@dataclass(frozen=True)
class EffectOrigin:
    session_id: str
    event_id: str
    tenant: str
    profile: str
    machine: str
    seal: str = field(repr=False)


def _origin_seal(parts: tuple[str, ...]) -> str:
    return hmac.new(_ORIGIN_KEY, json.dumps(parts, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def _observation_profile() -> Optional[str]:
    if (os.environ.get(ENV_MODE) or "").strip().lower() != "observe":
        return None
    settings = _settings()
    profile = settings.get("profile")
    if (not _config_disarms(settings) and settings.get("tenant") == "personal"
            and settings.get("machine") == "mini" and profile == "default"
            and profile == _active_profile()):
        return profile
    return None


def observation_enabled() -> bool:
    return _observation_profile() is not None


def native_input_enabled() -> bool:
    if observation_enabled():
        return True
    settings = _settings() if resolve_mode() else {}
    return (not _config_disarms(settings) and settings.get("tenant") == "personal"
            and settings.get("machine") == "mini" and settings.get("profile") == _active_profile())


def capture_effect_origin(session_id: str, event_id: str) -> Optional[EffectOrigin]:
    """Called only after upstream authentication; never synthesizes identity."""
    profile = _active_profile() if native_input_enabled() else None
    if profile is None:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
        return None
    parts = (session_id, event_id, "personal", profile, "mini")
    return EffectOrigin(*parts, _origin_seal(parts))


def current_effect_origin() -> Optional[EffectOrigin]:
    return _EFFECT_ORIGIN.get()


def publish_effect_origin(origin: Optional[EffectOrigin]) -> None:
    if origin is not None:
        parts = (origin.session_id, origin.event_id, origin.tenant, origin.profile, origin.machine)
        if not hmac.compare_digest(origin.seal, _origin_seal(parts)):
            raise ValueError("invalid effect origin seal")
    _EFFECT_ORIGIN.set(origin)


@contextmanager
def effect_origin_scope(origin: Optional[EffectOrigin]):
    token = _EFFECT_ORIGIN.set(None)
    try:
        publish_effect_origin(origin)
        yield
    finally:
        _EFFECT_ORIGIN.reset(token)


def _observation_uid() -> int:
    """Ownership has no fallback when the host cannot establish its UID."""
    getter = getattr(os, "getuid", None)
    if not callable(getter):
        raise OSError("observation ownership unavailable")
    try:
        uid = getter()
    except Exception as exc:
        raise OSError("observation ownership unavailable") from exc
    if type(uid) is not int or uid < 0:
        raise OSError("observation ownership invalid")
    return uid


class ObservationWriter:
    """Owner-only local CAS. Effect sequence never changes during settlement.

    A write failure latches this boot broken. A missing disk cannot be made
    observable by writing to it: the last checkpoint expires within 120s.
    """

    def __init__(self, root: Path, *, code_sha: str, boot_id: Optional[str] = None, profile: str = "default"):
        if profile != "default" or not root.is_absolute() or not re.fullmatch(r"[0-9a-f]{40}", code_sha):
            raise ValueError("observation configuration invalid")
        self.profile = profile
        self.root = root
        self.code_sha = code_sha
        self.boot_id = boot_id or str(uuid.uuid4())
        self.broken = False
        self.stopped = False
        self._mutex = threading.RLock()
        self._channel_fd = None

    def _claim_channel(self):
        """One live process/class owns a checkpoint stream until stopped."""
        import fcntl

        if self._channel_fd is not None:
            return
        fd = os.open(self.root / ".observation-channel-owner", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != _observation_uid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise OSError("observation owner unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._channel_fd = fd
        except BaseException:
            os.close(fd)
            raise

    def __del__(self):
        if getattr(self, "_channel_fd", None) is not None:
            os.close(self._channel_fd)
            self._channel_fd = None

    @contextmanager
    def _locked(self):
        with self._mutex:
            try:
                uid = _observation_uid()
                import fcntl

                for directory in (self.root, self.root / "observation-effects"):
                    if directory.is_symlink():
                        raise OSError("observation directory symlink")
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    info = directory.stat()
                    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
                        raise OSError("observation directory permissions")
                fd = os.open(self.root / ".observation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                            or stat.S_IMODE(info.st_mode) != 0o600):
                        raise OSError("observation lock unsafe")
                    deadline = time.monotonic() + 0.25
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("observation lock busy")
                            time.sleep(0.005)
                    yield
                finally:
                    os.close(fd)
            except OSError:
                self.broken = True
                raise

    def _read(self, path: Path) -> Optional[dict]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != _observation_uid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or info.st_size > 65536):
                raise OSError("observation file unsafe")
            document = json.load(stream)
        if not isinstance(document, dict):
            raise ValueError("observation file invalid")
        return document

    def _write(self, path: Path, document: dict):
        fd, temporary = tempfile.mkstemp(prefix=".observation-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def checkpoint(self, *, now: Optional[int] = None, stopped: bool = False):
        now = int(time.time()) if now is None else now
        self.stopped = self.stopped or stopped
        with self._locked():
            self._claim_channel()
            previous = self._read(self.root / "observation-channel.json") or {}
            records = [self._read(path) for path in (self.root / "observation-effects").glob("*.json")]
            last = max((r["sequence"] for r in records if r and r["boot_id"] == self.boot_id), default=0)
            self._write(self.root / "observation-channel.json", {
                "schema": "hermes.kernel-observation-channel/v1", "profile": self.profile, "machine": "mini",
                "code_sha": self.code_sha, "pid": os.getpid(), "boot_id": self.boot_id,
                "sequence": previous.get("sequence", 0) + 1,
                "state": "broken" if self.broken else "stopped" if self.stopped else "healthy" if previous.get("boot_id") == self.boot_id else "starting",
                "checked_at": now, "valid_until": now + 120, "last_effect_sequence": last,
            })
            if self.stopped:
                os.close(self._channel_fd)
                self._channel_fd = None

    def reserve(self, origin: EffectOrigin, destination: str, content: str, *, now: Optional[int] = None, include_created=False):
        with effect_origin_scope(origin):
            effect_id = hashlib.sha256(json.dumps([
                "hermes-effect.v1", origin.session_id, origin.event_id, origin.tenant,
                origin.profile, origin.machine, destination, content_sha256(content),
            ], separators=(",", ":")).encode()).hexdigest()
        now = int(time.time()) if now is None else now
        with self._locked():
            path = self.root / "observation-effects" / (effect_id + ".json")
            existing = self._read(path)
            if existing is not None:
                return (existing, False) if include_created else existing
            if self.broken or self.stopped:
                raise OSError("observation boot not healthy")
            records = [self._read(p) for p in path.parent.glob("*.json")]
            sequence = max((r["sequence"] for r in records if r and r["boot_id"] == self.boot_id), default=0) + 1
            record = {"schema": "hermes.kernel-effect-observation/v1", "effect_id": effect_id,
                      "profile": origin.profile, "machine": origin.machine, "boot_id": self.boot_id,
                      "sequence": sequence, "state": "reserved", "reserved_at": now, "updated_at": now,
                      "origin_session_id": origin.session_id, "settlement": None, "gap_reason": None}
            self._write(path, record)
            return (record, True) if include_created else record

    def transition(self, effect_id: str, state: str, *, settlement=None, gap_reason=None, now: Optional[int] = None):
        if not re.fullmatch(r"[0-9a-f]{64}", effect_id) or state not in {"settled", "gap"}:
            raise ValueError("invalid observation transition")
        with self._locked():
            path = self.root / "observation-effects" / (effect_id + ".json")
            record = self._read(path)
            if record is None:
                raise ValueError("observation reservation missing")
            if record["state"] != "reserved":
                if (record["state"], record["settlement"], record["gap_reason"]) == (state, settlement, gap_reason):
                    return record
                raise ValueError("observation terminal transition")
            if state == "gap" and (settlement is not None or gap_reason not in {
                "admission_failed", "delivery_ambiguous", "delivery_failed", "ack_missing", "ledger_failed",
                "spool_failed", "release_ambiguous", "release_failed", "identity_missing", "storage_failed",
            }):
                raise ValueError("invalid observation gap")
            if state == "settled" and (not isinstance(settlement, dict) or gap_reason is not None):
                raise ValueError("invalid observation settlement")
            if state == "settled":
                fields = {"work_id", "attempt_id", "generation", "delivery_id", "delivery_event_id", "ack_sha256",
                          "ledger_sha256", "spool_sha256", "release_event_id", "release_source_event_id", "release_receipt_sha256"}
                if (set(settlement) != fields or type(settlement["generation"]) is not int
                        or not 1 <= settlement["generation"] <= 9007199254740991
                        or any(not isinstance(settlement[k], str) or not _UUID7_RE.fullmatch(settlement[k])
                               for k in ("work_id", "attempt_id", "release_event_id"))
                        or any(not isinstance(settlement[k], str) or not re.fullmatch(r"[0-9a-f]{64}", settlement[k])
                               for k in ("ack_sha256", "ledger_sha256", "spool_sha256", "release_receipt_sha256"))
                        or any(not isinstance(settlement[k], str) or not re.fullmatch(r"[A-Za-z0-9._:@/-]{1,200}", settlement[k])
                               for k in ("delivery_id", "delivery_event_id"))
                        or not isinstance(settlement["release_source_event_id"], str)
                        or not re.fullmatch(r"kernel-effect-[0-9a-f]{64}:release-[1-9][0-9]{0,15}", settlement["release_source_event_id"])):
                    raise ValueError("invalid observation settlement")
            updated = dict(record, state=state, settlement=settlement, gap_reason=gap_reason,
                           updated_at=int(time.time()) if now is None else now)
            self._write(path, updated)
            return updated


_OBSERVER: Optional[ObservationWriter] = None


_OBSERVER_START_LOCK = threading.RLock()
_OBSERVER_HEARTBEAT = None
_OBSERVER_STOP = threading.Event()


def start_observation() -> Optional[ObservationWriter]:
    with _OBSERVER_START_LOCK:
        return _start_observation_locked()


def _start_observation_locked() -> Optional[ObservationWriter]:
    global _OBSERVER
    profile = _observation_profile()
    if profile is None:
        return None
    try:
        from hermes_cli.build_info import get_code_identity
        code_sha = get_code_identity().get("sha")
        configured = (_settings().get("observation") or {}).get("code_sha")
        if code_sha != configured:
            raise ValueError("observation code pin mismatch")
        root = Path(os.environ.get("HERMES_KERNEL_SHADOW_RECEIPT_DIR", ""))
        if _OBSERVER is not None:
            if _OBSERVER.root != root or _OBSERVER.code_sha != code_sha or _OBSERVER.profile != profile or _OBSERVER.stopped:
                raise ValueError("observation owner cannot be rebound")
            return _OBSERVER
        candidate = ObservationWriter(root, code_sha=code_sha, profile=profile)
        candidate.checkpoint()
        _OBSERVER = candidate
    except Exception:
        logger.error("observation startup unavailable; checkpoint must expire", exc_info=False)
        if _OBSERVER is not None:
            _OBSERVER.broken = True
    return _OBSERVER


def start_observation_heartbeat():
    """Serve's idle-independent writer, pinned to the effective home context."""
    global _OBSERVER_HEARTBEAT
    with _OBSERVER_START_LOCK:
        writer = start_observation()
        if writer is None or writer.broken or _OBSERVER_HEARTBEAT is not None:
            return writer
        context = contextvars.copy_context()
        def heartbeat():
            while not _OBSERVER_STOP.wait(30):
                observation_checkpoint()
        _OBSERVER_HEARTBEAT = threading.Thread(
            target=lambda: context.run(heartbeat), daemon=True, name="kernel-observation",
        )
        _OBSERVER_HEARTBEAT.start()
        return writer


def stop_observation_heartbeat():
    _OBSERVER_STOP.set()
    thread = _OBSERVER_HEARTBEAT
    if thread is not None:
        thread.join()
        observation_checkpoint(stopped=True)


def bind_native_prompt_source(session_id: str, source_event_id: str, text: str) -> bool:
    """Bind authenticated input identity to content without storing its payload.

    Called under the session's effective home. The ID is supplied by the
    authenticated ingress, never generated here or inferred from the text.
    """
    if not native_input_enabled() or not isinstance(text, str):
        return False
    if observation_enabled():
        writer = start_observation_heartbeat()
    else:
        # Reuse the owned CAS file operations only; enforcement does not claim
        # an observation channel or publish synthetic observer checkpoints.
        from hermes_cli.build_info import get_code_identity
        code_sha = get_code_identity().get("sha")
        try:
            writer = ObservationWriter(Path(_state_dir(_settings())) / "native-inputs", code_sha=code_sha)
        except (TypeError, ValueError):
            return False
    if writer is None or writer.broken or writer.stopped:
        return False
    try:
        source = uuid.UUID(source_event_id)
        if source.version != 4 or str(source) != source_event_id:
            return False
        if not _SESSION_ID_RE.fullmatch(session_id):
            return False
        binding = _digest("native-prompt.v1", session_id, source_event_id)
        document = {"schema": "hermes.native-prompt-binding/v1", "binding": binding,
                    "content_sha256": content_sha256(text)}
        with writer._locked():
            path = writer.root / (".native-prompt-" + binding + ".json")
            previous = writer._read(path)
            if previous is not None and previous != document:
                raise ValueError("native source conflict")
            if previous is None:
                writer._write(path, document)
        return True
    except Exception:
        observation_gap(None, "native_source_conflict_or_storage_failed")
        return False


def observation_checkpoint(*, stopped=False):
    if _OBSERVER is not None:
        try:
            _OBSERVER.checkpoint(stopped=stopped)
        except Exception:
            _OBSERVER.broken = True
            logger.error("observation storage unavailable; checkpoint must expire", exc_info=False)


def observation_gap(observation: Optional[dict], reason: str):
    if not observation:
        if _OBSERVER is not None:
            _OBSERVER.broken = True
            observation_checkpoint()
        logger.error("effect observation unavailable: %s", reason)
        return
    try:
        writer = ObservationWriter(Path(observation["root"]), code_sha=observation["code_sha"], boot_id=observation["effect"]["boot_id"])
        with writer._locked():
            prior = writer._read(writer.root / "observation-effects" / (observation["effect"]["effect_id"] + ".json"))
            if prior and prior["state"] in {"gap", "settled"}:
                return  # Preserve the first terminal observation; never rewrite it.
        writer.transition(observation["effect"]["effect_id"], "gap", gap_reason=reason)
    except Exception:
        if _OBSERVER is not None:
            _OBSERVER.broken = True
            observation_checkpoint()
        logger.error("effect observation gap persistence failed: %s", reason)


def reserve_observed_effect(destination: str, content: str):
    """Observation never supplies delivery authorization or changes legacy send."""
    if not observation_enabled():
        return None
    origin = current_effect_origin()
    if origin is None or _OBSERVER is None:
        observation_gap(None, "identity_missing" if origin is None else "storage_failed")
        return None
    try:
        effect, created = _OBSERVER.reserve(origin, destination, content, include_created=True)
        return {"root": str(_OBSERVER.root), "code_sha": _OBSERVER.code_sha, "effect": effect, "created": created}
    except Exception:
        observation_gap(None, "storage_failed")
        return None


def admit_observed_effect(observation: dict) -> Optional[dict]:
    origin = current_effect_origin()
    if origin is None or not observation_enabled():
        return None
    outcome = _run_admission(session_id=origin.session_id,
        request_text="message_agent effect " + observation["effect"]["effect_id"],
        seam=SEAM_EXECUTION, event_id="effect-" + observation["effect"]["effect_id"],
        event_kind="chat_message", declares_execution=True, _observing=True)
    if outcome.state != STATE_ADMITTED or outcome.turn_identity is None or not outcome.authority_pin_matched:
        observation_gap(observation, "admission_failed")
        return None
    return dict(outcome.turn_identity)


def new_release_event_id() -> str:
    return str(uuid.UUID(int=((int(time.time() * 1000) & ((1 << 48) - 1)) << 80)
        | (7 << 76) | (secrets.randbits(12) << 64) | (2 << 62) | secrets.randbits(62)))


def release_observed_effect(identity: Mapping[str, Any], release_event_id: str) -> dict:
    """One exact release call; timeout/invalid response is ambiguous, never retried."""
    identity = _validate_turn_identity(identity)
    if not _UUID7_RE.fullmatch(release_event_id):
        raise TurnIdentityError("release_event_invalid")
    roots = _resolve_trust_roots()
    settings = _settings()
    base_url = str(settings.get("jarvis_base_url") or "").strip()
    secret_ref = str(settings.get("secret_ref") or "").strip()
    if not base_url or not secret_ref or settings.get("transport_fixture"):
        raise TurnIdentityError("turn_identity_requires_remote")
    result = subprocess.run([roots.admitter_bin, "release-turn-identity", "--stdin",
        "--jarvis-base-url", base_url, "--secret-ref", secret_ref],
        input=_canonical({"turn_identity": identity, "release_event_id": release_event_id}),
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=roots.root_dir,
        env=_child_env(settings), shell=False)
    if result.returncode != EXIT_OK:
        raise TurnIdentityError("release_unavailable")
    receipt = json.loads(result.stdout or "")
    session_key = "hk1-" + hashlib.sha256(json.dumps([
        "hermes-kernel-execution.v1", identity["work_id"], identity["origin_session_id"],
    ], separators=(",", ":")).encode()).hexdigest()
    source_event = "kernel-effect-" + hashlib.sha256(json.dumps([
        "hermes-kernel-effect-release.v1", identity["work_id"], identity["attempt_id"],
        identity["generation"], session_key, release_event_id,
    ], separators=(",", ":")).encode()).hexdigest() + ":release-" + str(identity["generation"])
    expected = {"schema_version": "work-control.effect-release.v1", "contract_version": "work-control.v1",
        "source_event_id": source_event, "work_id": identity["work_id"],
        "origin_session_id": identity["origin_session_id"], "execution_session_key": session_key,
        "attempt_id": identity["attempt_id"], "generation": identity["generation"],
        "release_event_id": release_event_id}
    if (not isinstance(receipt, dict) or set(receipt) != set(expected) | {"outcome"}
            or any(receipt[k] != v for k, v in expected.items())
            or receipt["outcome"] not in {"released", "already_released"}):
        raise TurnIdentityError("release_receipt_rebind")
    return receipt

# --------------------------------------------------------------------------- #
# Trust roots — the environment says WHERE, these constants say WHICH
# --------------------------------------------------------------------------- #
ENV_ADMITTER_BIN = "HERMES_KERNEL_ADMITTER_BIN"
ENV_SCHEMA_PATH = "HERMES_KERNEL_SCHEMA_PATH"
ENV_SCHEMA_SHA256 = "HERMES_KERNEL_SCHEMA_SHA256"

# The exact K7 build this consumer was approved against.
# Retained for the explicitly gated offline fixture suite. Production accepts
# only TURN_IDENTITY_ADMITTER_SHA256 below.
ADMITTER_SHA256 = "9110be8580acdaf0acee3b3fd0df149523b9f189c3a0c0898f5b90510c6c2a99"
TURN_IDENTITY_ADMITTER_SHA256 = "69cb5d2ce9486b22145b1ab8d80399f9ba866b619332e8ffeb924c4d8475f03b"
SCHEMA_SHA256 = "92ce749bf6bfecf3528b83da9a5ef716f4ac6f7795342e2bf70e094eff306a3b"
TURN_IDENTITY_SCHEMA_VERSION = "hermes.kernel-turn-identity.v1"
TURN_FENCE_SCHEMA_VERSION = "hermes.kernel-turn-fence.v1"
TURN_IDENTITY_ATTESTATION_VERSION = "hermes-kernel-turn-identity-attestation.v1"
K7_BUNDLE_SHA256 = {
    "control/kernel/admit_cli.py": TURN_IDENTITY_ADMITTER_SHA256,
    "control/kernel/contracts.py": "dc1f7e818fb967a0d7eb8e5ac82e424c957e4623827c1746e692c164f828cc19",
    "control/kernel/admitter.py": "8061b97b1798722e62e49e34d0dce9ad12fe0b54939d94a8ebe7baabb81ac4c7",
    "control/kernel/client.py": "7a61d2548fcb10f7bf93256d9833d6dd7b7812f6de98e4ebc2c0e7605764bdb7",
    "control/kernel/native_keychain.py": "ae76dbe9e88ecb8a2b7d54595852f3e51a07a36d4009d73aea04cb021ab5c057",
    "control/kernel/inbox.py": "cf906a4f30285958a7aff3d3509eaa6b845c2ac68c5ef8136c8e47f4805df23f",
    "control/kernel/projector.py": "292f64cae244b90b56ec649e58b1487155c3dac5eab4af4f4b91c50a30806315",
    "control/kernel/store.py": "141577fce9456173b981ea33e2414af679fee4d574330040515abb8295c69af1",
    "control/schemas/work-envelope.schema.json": SCHEMA_SHA256,
    "control/schemas/kernel-turn-identity.schema.json": "686ca4d5ef12877ff34b4edb258c92c79b10594359223880d19ea24f339e1a35",
}

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
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

CONFIG_SECTION = ("agent", "durable_admission")

# The external dispatcher a long/promised execution is handed off to. Named as
# data so the refusal is stable and the operator sees one string everywhere.
EXTERNAL_WORK_DISPATCHER = "hermes-work-control"


class _State(threading.local):
    outcome: Optional["AdmissionOutcome"] = None


_state = _State()

# The verified pre-admission produced at the gateway ingress and consumed once,
# further in.
#
# It is a SHARED MUTABLE BOX, not a ContextVar, and that is the whole point.
# The gateway hands blocking turn work to a thread pool through
# ``_run_in_executor_with_context``, which runs the worker under
# ``copy_context()``. A ContextVar cleared inside that copy does not clear the
# async parent's — so a carrier built on one is not one-shot: a second executor
# call in the SAME request re-reads an admission that was already spent, and a
# second model invocation rides it. A box is shared by reference, so the take
# is visible to the parent that owns the lifecycle.
#
# The ContextVar below carries only the BOX, so paths that never touch the
# executor still find it; the one-shot guarantee lives in the box itself.
_PRE_ADMISSION: contextvars.ContextVar[Optional["PreAdmission"]] = (
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
    authority_pin_matched: bool
    seal: str = field(repr=False)

    def sealed_core(self) -> Dict[str, Any]:
        return {
            "work_id": self.work_id,
            "idempotency_key": self.idempotency_key,
            "seam": self.seam,
            "content_sha256": self.content_sha256,
            "request_key": self.request_key,
            "admitted_at": self.admitted_at,
            "authority_pin_matched": self.authority_pin_matched,
        }

    def as_dict(self) -> Dict[str, Any]:
        """Plain data for logs/persistence. Carries no seal, so it is inert."""
        return dict(self.sealed_core())


def _seal_for(core: Mapping[str, Any]) -> str:
    return hmac.new(_SEAL_KEY, _canonical(dict(core)).encode("utf-8"), hashlib.sha256).hexdigest()


def _turn_identity_seal(identity: Mapping[str, Any]) -> str:
    return hmac.new(
        _SEAL_KEY,
        (TURN_IDENTITY_SCHEMA_VERSION + "\0" + _canonical(dict(identity))).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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
    authority_pin_matched: bool = False

    def payload(self) -> Dict[str, Any]:
        """The stable, typed shape a tool returns. Never says 'dispatched'.

        ``authority_verification_required`` is always true: K8 compares the
        public AuthorityPin fields only, so whatever it hands over is an
        unauthenticated candidate. The dispatcher must authenticate the
        attestation before acting on it.
        """
        return {
            "status": "handoff_required",
            "handoff": {
                "dispatcher": self.dispatcher,
                "reason": self.reason_code,
                "work_id": self.work_id,
                "request_key": self.request_key,
                "authority_pin_matched": self.authority_pin_matched,
                "authority_verified": False,
                "authority_verification_required": True,
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
    authority_version: Optional[int] = None
    idempotency_key: Optional[str] = None
    content_sha256: Optional[str] = None
    request_key: Optional[str] = None
    event_id: Optional[str] = None
    session_id: Optional[str] = None
    model_may_run: bool = False
    kernel_effects_allowed: bool = False
    # The public pin fields matched. NOT an authentication — see
    # _authority_pin_matched.
    authority_pin_matched: bool = False
    signal_persisted: bool = False
    replayed: bool = False
    retryable: bool = False
    mode_off: bool = False
    admitted_at: int = 0
    receipt: Optional[Dict[str, Any]] = field(default=None, repr=False)
    turn_identity: Optional[Dict[str, Any]] = field(default=None, repr=False)
    turn_identity_seal: Optional[str] = field(default=None, repr=False)

    @property
    def blocked(self) -> bool:
        return not self.mode_off and not self.model_may_run

    @property
    def authority_verified(self) -> bool:
        """Always False in K8, and it is a property so it cannot be assigned.

        Verification means recomputing the attestation HMAC against the pinned
        authority's credential. K8 cannot do that — it compares public fields
        only — so claiming verification here would be a lie that a downstream
        gate would act on. K9 owns this.
        """
        return False

    @property
    def effects_allowed(self) -> bool:
        """No effect is ever released by K8.

        An effect needs a Work whose issuing authority was AUTHENTICATED, and
        K8 cannot authenticate one. A matched pin is a candidate, not a
        capability, so this stays False even for a receipt the kernel itself
        marked ``effects_allowed`` — long work is handed off instead.
        """
        return False

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
            authority_pin_matched=self.authority_pin_matched,
        )


class PreAdmission:
    """A one-shot carrier for one admitted request.

    Shared by reference across the executor boundary, so ``take()`` is visible
    to the async parent that owns the turn's lifecycle. Thread-safe because the
    taker and the invalidator genuinely run on different threads.
    """

    __slots__ = ("_outcome", "_lock", "_taken", "_invalidated")

    def __init__(self, outcome: "AdmissionOutcome") -> None:
        self._outcome = outcome
        self._lock = threading.Lock()
        self._taken = False
        self._invalidated = False

    def take(self) -> Optional["AdmissionOutcome"]:
        """Return the outcome exactly once, ever."""
        with self._lock:
            if self._taken or self._invalidated:
                return None
            self._taken = True
            return self._outcome

    def peek(self) -> Optional["AdmissionOutcome"]:
        """Read without spending. For lifecycle decisions, never for release."""
        with self._lock:
            return None if (self._taken or self._invalidated) else self._outcome

    def invalidate(self) -> None:
        """Retire the carrier. Idempotent, and safe to call from any thread.

        The parent calls this in its own ``finally`` for every exit — success,
        refusal, exception, cancellation, proxy, background and retry — so a
        carrier can never outlive the request that minted it.
        """
        with self._lock:
            self._invalidated = True
            self._outcome = None

    @property
    def spent(self) -> bool:
        with self._lock:
            return self._taken or self._invalidated


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
        authority_pin_matched=False,
        signal_persisted=signal_persisted,
        retryable=retryable,
        **extra,
    )


# --------------------------------------------------------------------------- #
# The mode gate — one environment read, no side effects of any kind
# --------------------------------------------------------------------------- #


class ModeError(RuntimeError):
    """The mode setting is not one this build understands."""

    reason_code = "kernel_mode_invalid"


class TurnIdentityError(RuntimeError):
    """The request-scoped K7 identity or its fresh fence is not authoritative."""


_TURN_IDENTITY_FIELDS = frozenset({
    "schema_version", "work_id", "authority_version", "tenant", "profile", "source",
    "origin_session_id", "origin_machine", "source_event_id", "source_event_observed_at",
    "request_key", "attempt_id", "generation", "authority_source", "authority_root_id",
    "attestation",
})


def _validate_turn_identity(document: Any) -> Dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _TURN_IDENTITY_FIELDS:
        raise TurnIdentityError("turn_identity_fields_invalid")
    identity = dict(document)
    if identity["schema_version"] != TURN_IDENTITY_SCHEMA_VERSION:
        raise TurnIdentityError("turn_identity_schema_invalid")
    if not _UUID_RE.fullmatch(str(identity["work_id"])) or not _UUID7_RE.fullmatch(str(identity["attempt_id"])):
        raise TurnIdentityError("turn_identity_work_attempt_invalid")
    # The authenticated K7 envelope owns tenant/profile/source.  `default` was
    # the original single-profile deployment, not a security boundary: keeping
    # it hard-coded makes an attested Kindra turn impossible to revalidate.
    # Accept only canonical profile names and require source to equal profile,
    # so a caller cannot rebind a valid personal identity to another profile.
    if (
        identity["tenant"] != "personal"
        or not _PROFILE_RE.fullmatch(str(identity["profile"]))
        or identity["source"] != identity["profile"]
    ):
        raise TurnIdentityError("turn_identity_scope_invalid")
    if identity["authority_source"] != AUTHORITY_REMOTE or not _ROOT_ID_RE.fullmatch(str(identity["authority_root_id"])):
        raise TurnIdentityError("turn_identity_authority_invalid")
    for name in ("authority_version", "source_event_observed_at", "generation"):
        value = identity[name]
        minimum = 1 if name in ("authority_version", "generation") else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise TurnIdentityError("turn_identity_integer_invalid")
    for name, pattern in (("origin_session_id", _SESSION_ID_RE), ("origin_machine", _MACHINE_RE),
                          ("source_event_id", _EVENT_ID_RE), ("request_key", _REQUEST_KEY_RE)):
        if not pattern.fullmatch(str(identity[name])):
            raise TurnIdentityError("turn_identity_binding_invalid")
    seal = identity["attestation"]
    if not isinstance(seal, Mapping) or set(seal) != {"alg", "version", "key_id", "value"}:
        raise TurnIdentityError("turn_identity_attestation_invalid")
    if (seal.get("alg") != ATTESTATION_ALG or seal.get("version") != TURN_IDENTITY_ATTESTATION_VERSION
            or not _KEY_ID_RE.fullmatch(str(seal.get("key_id") or ""))
            or not _SHA256_RE.fullmatch(str(seal.get("value") or ""))):
        raise TurnIdentityError("turn_identity_attestation_invalid")
    return identity


def turn_identity_sha256(document: Mapping[str, Any]) -> str:
    identity = _validate_turn_identity(document)
    return hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()


def resolve_mode(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Armed? One environment read, no side effects of any kind.

    Arming is explicit: ``HERMES_KERNEL_V1_MODE`` must say so. Pointing
    ``HERMES_KERNEL_ADMITTER_BIN`` at a kernel does NOT arm anything — a path is
    where the kernel lives, not a decision to enforce with it.

    An UNRECOGNISED value raises. Treating a typo as "off" is a fail-open: an
    operator who wrote ``enfroce`` asked for enforcement and would silently get
    none. Refusing is the only reading that cannot quietly disable the gate,
    and it happens here — before any config read, any subprocess and any model.

    A valid "off" returns False having done nothing at all. "observe" also
    returns False here: it never arms ingress or legacy enforcement. Its
    separately scoped observer runs only at the actual effect boundary.
    """
    env = os.environ if environ is None else environ
    mode = (env.get(ENV_MODE) or "").strip().lower()
    if mode in _ARMING_MODES:
        return True
    if mode in _OFF_MODES or mode == "observe":
        return False
    raise ModeError(
        "%s=%r is not a recognised mode (expected one of %s, or one of %s to "
        "disable); refusing rather than guessing which the operator meant"
        % (ENV_MODE, mode, sorted(_ARMING_MODES), sorted(m for m in _OFF_MODES if m))
    )


def admission_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Armed? Answers False for a valid off, True for a valid arming value.

    An invalid mode is NOT a boolean question, so it propagates as
    :class:`ModeError` rather than collapsing into False.
    """
    return resolve_mode(environ)


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def last_outcome() -> Optional[AdmissionOutcome]:
    return getattr(_state, "outcome", None)


def reset_state_for_tests() -> None:
    _state.outcome = None
    clear_pre_admission()
    _EXECUTION.set(None)
    _ADMITTED_TURN.set(None)


def _remember(outcome: AdmissionOutcome) -> AdmissionOutcome:
    _state.outcome = outcome
    return outcome


# --------------------------------------------------------------------------- #
# Request-scoped carriers
# --------------------------------------------------------------------------- #


def publish_pre_admission(outcome: AdmissionOutcome) -> "PreAdmission":
    """Publish the gateway's verified admission and return its one-shot carrier.

    The caller OWNS the returned carrier: it transports it explicitly to the one
    ``run_conversation`` it belongs to, and invalidates it when the request ends.
    The ContextVar is a convenience for in-thread paths, not the guarantee.
    """
    # Retire whatever this context still holds before minting a new one. A
    # previous request's box may still be referenced by a worker that never
    # took it; replacing the ContextVar alone would leave that reference live.
    previous = _PRE_ADMISSION.get()
    if previous is not None:
        previous.invalidate()
    carrier = PreAdmission(outcome)
    _PRE_ADMISSION.set(carrier)
    return carrier


def current_pre_admission() -> Optional["PreAdmission"]:
    """The carrier published on this context, spent or not."""
    return _PRE_ADMISSION.get()


def clear_pre_admission() -> None:
    """Retire whatever carrier this context holds. Parent-side lifecycle only."""
    carrier = _PRE_ADMISSION.get()
    if carrier is not None:
        carrier.invalidate()
    _PRE_ADMISSION.set(None)


def consume_pre_admission(carrier: Optional["PreAdmission"] = None) -> Optional[AdmissionOutcome]:
    """Take the pre-admission exactly once.

    ``carrier`` is the explicitly transported one — the only form that survives
    the executor boundary correctly. Falling back to the ContextVar keeps the
    in-thread paths working; the box is what makes both one-shot.
    """
    if carrier is None:
        carrier = _PRE_ADMISSION.get()
    if carrier is None:
        return None
    return carrier.take()


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


@contextmanager
def admitted_turn_scope(outcome: AdmissionOutcome):
    """Give one tool its own admission without replacing its parent turn."""
    token = _ADMITTED_TURN.set(outcome)
    try:
        yield
    finally:
        _ADMITTED_TURN.reset(token)


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
    fixture_legacy = (
        env.get(ENV_K7_TEST_FIXTURE) == "1" and actual_bin == ADMITTER_SHA256
    )
    if actual_bin != TURN_IDENTITY_ADMITTER_SHA256 and not fixture_legacy:
        # A different kernel binary is a different kernel, whatever the path
        # says. Refuse before executing it, not after reading its answer.
        raise _TrustRootError(
            "admitter_hash_mismatch",
            "%s hashes to %s; this consumer is pinned to %s"
            % (bin_path, actual_bin, TURN_IDENTITY_ADMITTER_SHA256),
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

    root_dir = bin_path.resolve().parents[2]
    if not fixture_legacy:
        for relative, expected in K7_BUNDLE_SHA256.items():
            member = root_dir / relative
            if member.is_symlink() or not member.is_file():
                raise _TrustRootError("k7_bundle_member_invalid", relative)
            actual = _sha256_file(member)
            if actual != expected:
                raise _TrustRootError(
                    "k7_bundle_hash_mismatch",
                    "%s hashes to %s; pinned %s" % (relative, actual, expected),
                )

    return _TrustRoots(str(bin_path), str(schema_path), str(root_dir))


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


def _authority_pin_matched(document: Mapping[str, Any], pin: Optional[Mapping[str, str]]) -> bool:
    """Compare the PUBLIC pin fields a forger controls, in K7's order.

    ⚠️ This is ONLY the pin-comparison half of K7's
    ``verify_pinned_attestation``, and its name says so. The half that actually
    authenticates — recomputing the HMAC over the attested core — needs the
    credential behind ``secret_ref``, which this package may not hold. Every
    field compared here is public data printed on the receipt itself.

    So a receipt that passes is an UNAUTHENTICATED CANDIDATE, never a verified
    one. ``authority_verified`` stays False unconditionally, no capability is
    released on the strength of a match, and the handoff carries
    ``authority_verification_required`` so K9 is told what is still owed.
    Calling a public-field comparison "verified" is exactly how a hand-written
    JSON with an invented work_id once unlocked an enforced gate.
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
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT",
    "HERMES_KERNEL_KEYCHAIN_HELPER", "HERMES_KERNEL_KEYCHAIN_HELPER_SHA256",
    "HERMES_KERNEL_KEYCHAIN_PATH",
)


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


def _run_turn_identity_revalidation(identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Ask the pinned K7 binary for a fresh exact-current fence."""
    roots = _resolve_trust_roots()
    settings = _settings()
    base_url = str(settings.get("jarvis_base_url") or "").strip()
    secret_ref = str(settings.get("secret_ref") or "").strip()
    if not base_url or not secret_ref or settings.get("transport_fixture"):
        raise TurnIdentityError("turn_identity_requires_remote")
    argv = [
        roots.admitter_bin, "revalidate-turn-identity", "--stdin", "--now",
        str(int(time.time())), "--jarvis-base-url", base_url, "--secret-ref", secret_ref,
    ]
    try:
        completed = subprocess.run(
            argv, input=_canonical(dict(identity)), capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, cwd=roots.root_dir, env=_child_env(settings), shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TurnIdentityError("turn_identity_revalidation_unavailable") from exc
    if completed.returncode != EXIT_OK:
        raise TurnIdentityError("turn_identity_stale")
    try:
        fence = json.loads(completed.stdout or "")
    except (TypeError, ValueError) as exc:
        raise TurnIdentityError("turn_identity_fence_unreadable") from exc
    expected_sha = turn_identity_sha256(identity)
    required = {
        "schema_version": TURN_FENCE_SCHEMA_VERSION,
        "fence_valid": True,
        "work_id": identity["work_id"],
        "attempt_id": identity["attempt_id"],
        "generation": identity["generation"],
        "identity_sha256": expected_sha,
    }
    if not isinstance(fence, dict) or any(fence.get(k) != v for k, v in required.items()):
        raise TurnIdentityError("turn_identity_fence_rebind")
    checked_at = fence.get("checked_at")
    if isinstance(checked_at, bool) or not isinstance(checked_at, int) or checked_at < 1:
        raise TurnIdentityError("turn_identity_fence_invalid")
    return dict(fence)


def revalidate_current_turn_identity() -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return this turn's verified identity and a fresh remote fence, or stop."""
    outcome = current_admitted_turn()
    if outcome is None or outcome.state != STATE_ADMITTED:
        raise TurnIdentityError("turn_identity_not_admitted")
    identity = _validate_turn_identity(outcome.turn_identity)
    expected_seal = _turn_identity_seal(identity)
    if not isinstance(outcome.turn_identity_seal, str) or not hmac.compare_digest(
        outcome.turn_identity_seal, expected_seal
    ):
        raise TurnIdentityError("turn_identity_carrier_invalid")
    expected = {
        "work_id": outcome.work_id,
        "authority_version": outcome.authority_version,
        "origin_session_id": outcome.session_id,
        "source_event_id": outcome.event_id,
        "request_key": outcome.request_key,
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise TurnIdentityError("turn_identity_rebind")
    # The remote seal authorizes one profile, not every profile sharing the
    # same Work Control credential.  Bind it again at the local execution seam
    # so a valid identity from another Hermes profile cannot be replayed here.
    if identity["profile"] != _active_profile() or identity["source"] != _active_profile():
        raise TurnIdentityError("turn_identity_profile_rebind")
    fence = _run_turn_identity_revalidation(identity)
    return identity, fence


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
    identity = document.get("turn_identity")
    if identity is not None:
        try:
            identity = _validate_turn_identity(identity)
        except TurnIdentityError:
            return "turn_identity_invalid"
        expected_identity = {
            "work_id": document.get("work_id"),
            "authority_version": document.get("authority_version"),
            "tenant": envelope["tenant"],
            "profile": envelope["profile"],
            "origin_session_id": origin["session_id"],
            "origin_machine": origin["machine"],
            "source_event_id": event["event_id"],
            "source_event_observed_at": event["observed_at"],
            "request_key": envelope["binding"]["request_key"],
            "authority_source": document.get("authority_source"),
            "authority_root_id": document.get("authority_root_id"),
        }
        if any(identity.get(key) != expected for key, expected in expected_identity.items()):
            return "turn_identity_rebind"
    if document.get("turn_identity_required") and identity is None:
        return "turn_identity_missing"
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
    _observing: bool = False,
) -> AdmissionOutcome:
    try:
        armed = resolve_mode() or (_observing and observation_enabled() and current_effect_origin() is not None)
    except ModeError as exc:
        # Before config, before subprocess, before the model.
        logger.error("durable admission mode refused: %s", exc)
        return _remember(
            _refuse(ModeError.reason_code, str(exc), seam=seam, session_id=session_id)
        )
    if not armed:
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
    turn_identity = document.get("turn_identity")
    return _remember(
        AdmissionOutcome(
            state=state,
            reason_code=str(document.get("reason_code") or ""),
            detail=str(document.get("detail") or ""),
            work_id=work_id if isinstance(work_id, str) and work_id.strip() else None,
            authority_version=(document.get("authority_version")
                               if isinstance(document.get("authority_version"), int) else None),
            idempotency_key=document.get("idempotency_key"),
            model_may_run=bool(document.get("model_may_run")),
            kernel_effects_allowed=bool(document.get("effects_allowed")),
            authority_pin_matched=_authority_pin_matched(document, _authority_pin(settings)),
            signal_persisted=signal_persisted,
            replayed=bool(document.get("replayed")),
            admitted_at=int(now),
            receipt=document,
            turn_identity=(dict(turn_identity) if isinstance(turn_identity, dict) else None),
            turn_identity_seal=(_turn_identity_seal(turn_identity)
                                if isinstance(turn_identity, dict) else None),
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
        authority_pin_matched=outcome.authority_pin_matched,
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
    try:
        if not resolve_mode():
            return None
    except ModeError as exc:
        # A misconfigured mode must not read as "off".
        logger.error("durable admission mode refused: %s", exc)
        return blocked_turn_result(
            agent,
            _refuse(ModeError.reason_code, str(exc)),
            conversation_history,
        )
    if getattr(agent, "_delegate_depth", 0) or getattr(agent, "_subagent_id", None):
        return None

    # The gateway transports its one-shot carrier explicitly (TurnContext ->
    # the agent, for exactly this call). Prefer it over the ContextVar: turn
    # work runs under copy_context(), where the ContextVar is a copy and only
    # the shared box is authoritative.
    pre = consume_pre_admission(getattr(agent, "_k8_pre_admission", None))
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
    origin = current_effect_origin()
    event_id = (origin.event_id if origin is not None and origin.session_id == session_id
                and origin.profile == _active_profile() else
                "turn.%s" % _digest(session_id, content_sha256(text))[:48])
    outcome = admit_input(
        session_id=session_id,
        request_text=text,
        event_id=event_id,
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
    "admitter_sha256": TURN_IDENTITY_ADMITTER_SHA256,
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
    "authority": "K8 compares PUBLIC AuthorityPin fields only "
    "(authority_pin_matched). It never authenticates: authority_verified is a "
    "read-only False, effects_allowed is a read-only False, and no work "
    "receipt capability is released. Every handoff carries "
    "authority_verification_required=true.",
    "carrier": "one-shot PreAdmission box, transported on "
    "gateway.turn_context.TurnContext.k8_pre_admission and retired by the "
    "parent frame; never a ContextVar cleared inside a copied executor context",
    "still_open_for_k9": (
        "recompute the attestation HMAC against the pinned AuthorityPin and the "
        "credential behind its secret_ref; until then nothing K8 emits may "
        "release an effect, and authority_verified stays a read-only False",
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
