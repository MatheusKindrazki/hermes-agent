"""Durable observation channel ownership and effect settlement storage."""
from __future__ import annotations
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent.durable_admission import EffectOrigin

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
        with os.fdopen(fd, encoding="utf-8-sig") as stream:
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
        from agent.durable_admission import effect_origin_scope, content_sha256

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
        from agent.durable_admission import _UUID7_RE

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
