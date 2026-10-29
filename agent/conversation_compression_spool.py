"""Durable turn handoff and lease-fenced continuation during compression."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

class DurableCompressionTurnSpool:
    """Append-only durable handoff for turns racing context compression.

    Records are atomically replaced only to advance ``pending`` → ``drained``;
    they are never deleted. A root-scoped sequence preserves enqueue order,
    while per-session monotonic epochs fence compression lineage publication.
    """

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, root: Path | str) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))

    @staticmethod
    def _directory_flags() -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return flags

    def _open_root_path_fd(self, *, create: bool) -> int:
        """Walk the absolute root one no-follow directory component at a time."""
        parts = self.root.parts
        if not parts or parts[0] != os.sep or any(part in {"", ".", ".."} for part in parts[1:]):
            raise RuntimeError("compression spool root must be an absolute safe path")
        current_fd = os.open(os.sep, self._directory_flags())
        try:
            for index, part in enumerate(parts[1:]):
                try:
                    next_fd = os.open(
                        part, self._directory_flags(), dir_fd=current_fd
                    )
                except FileNotFoundError:
                    if not create:
                        raise RuntimeError(
                            "compression spool canonical root is missing"
                        )
                    try:
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        # Another process won the same safe component create.
                        # The no-follow open below revalidates what appeared.
                        pass
                    next_fd = os.open(
                        part, self._directory_flags(), dir_fd=current_fd
                    )
                except OSError as exc:
                    raise RuntimeError(
                        "compression spool path contains a symlink or non-directory"
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
                if index == len(parts[1:]) - 1:
                    os.fchmod(current_fd, 0o700)
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def _ensure_root(self) -> None:
        root_fd = self._open_root_path_fd(create=True)
        os.close(root_fd)

    def _open_root_fd(self) -> int:
        """Open and pin the validated spool directory for one operation."""
        fd = self._open_root_path_fd(create=True)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise RuntimeError("compression spool root is not a directory")
        return fd

    def _assert_canonical_root(self, root_fd: int, record_name: Optional[str] = None) -> None:
        canonical_fd = self._open_root_path_fd(create=False)
        try:
            pinned = os.fstat(root_fd)
            canonical = os.fstat(canonical_fd)
            if (pinned.st_dev, pinned.st_ino) != (canonical.st_dev, canonical.st_ino):
                raise RuntimeError("compression spool canonical root changed")
            if record_name is not None:
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                record_fd = os.open(record_name, flags, dir_fd=canonical_fd)
                try:
                    if not stat.S_ISREG(os.fstat(record_fd).st_mode):
                        raise RuntimeError(
                            "compression spool canonical record is not regular"
                        )
                finally:
                    os.close(record_fd)
        finally:
            os.close(canonical_fd)

    @staticmethod
    def _assert_regular_no_symlink(path: Path, *, allow_missing: bool) -> bool:
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            if allow_missing:
                return False
            raise
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise RuntimeError(f"compression spool path is not a regular file: {path.name}")
        return True

    @staticmethod
    def _key(session_id: str) -> str:
        return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]

    @contextlib.contextmanager
    def _locked(self, scope: str):
        root_fd = self._open_root_fd()
        root_stat = os.fstat(root_fd)
        lock_key = f"{root_stat.st_dev}:{root_stat.st_ino}::{scope}"
        with self._locks_guard:
            local_lock = self._locks.setdefault(lock_key, threading.RLock())
        try:
            local_context = local_lock
            with local_context:
                lock_name = f".lock-{self._key(scope)}"
                flags = os.O_CREAT | os.O_RDWR
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                lock_fd = os.open(lock_name, flags, 0o600, dir_fd=root_fd)
                os.fchmod(lock_fd, 0o600)
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    os.close(lock_fd)
                    raise RuntimeError("compression spool lock is not a regular file")
                lock_file = os.fdopen(lock_fd, "a+b")
                acquired = False
                try:
                    try:
                        import fcntl
                    except ImportError as exc:
                        raise RuntimeError("compression spool requires fcntl locking") from exc
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    acquired = True
                    yield root_fd
                finally:
                    try:
                        if acquired:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    finally:
                        lock_file.close()
        finally:
            os.close(root_fd)

    @contextlib.contextmanager
    def _lineage_locked(self, session_id: str):
        """Fence lineage reads/writes with one cross-process lock order.

        Lock order is always ``global lineage`` then ``session state``.
        Enqueue/drain only need the outer global fence; every state writer
        enters through this helper. No code may acquire global while holding a
        session lock, preventing nested inversion across threads/processes.
        """
        with self._locked("global"):
            with self._locked(f"state:{session_id}") as root_fd:
                yield root_fd

    @staticmethod
    def _relative_name(path: Path | str) -> str:
        name = Path(path).name
        if name != str(path) and Path(path).parent != Path("."):
            # Public helpers pass absolute Paths rooted at self.root. Only the
            # basename is used after the root fd has been pinned.
            name = Path(path).name
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise RuntimeError("invalid compression spool filename")
        return name

    def _write_json(
        self, path: Path | str, payload: dict[str, Any], *, root_fd: Optional[int] = None
    ) -> None:
        own_fd = root_fd is None
        if own_fd:
            root_fd = self._open_root_fd()
        assert root_fd is not None
        name = self._relative_name(path)
        temp_name = f".compression-turn-{uuid.uuid4().hex}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temp_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            if own_fd:
                os.close(root_fd)

    def _read_json(
        self, path: Path | str, *, root_fd: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        own_fd = root_fd is None
        if own_fd:
            root_fd = self._open_root_fd()
        assert root_fd is not None
        name = self._relative_name(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=root_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RuntimeError(
                        f"compression spool path changed during validation: {name}"
                    )
                with os.fdopen(fd, "r", encoding="utf-8-sig") as handle:
                    fd = -1
                    value = json.load(handle)
            finally:
                if fd >= 0:
                    os.close(fd)
        except FileNotFoundError:
            return None
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"corrupt compression spool JSON: {name}") from exc
        finally:
            if own_fd:
                os.close(root_fd)
        if not isinstance(value, dict):
            raise RuntimeError(f"corrupt compression spool JSON: {name}")
        return value

    def _resync_file(self, path: Path | str, *, root_fd: int) -> None:
        name = self._relative_name(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(name, flags, dir_fd=root_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"compression spool path is not regular: {name}")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(root_fd)

    def _state_path(self, session_id: str) -> Path:
        return self.root / f"state-{self._key(session_id)}.json"

    def _state(self, session_id: str, *, root_fd: Optional[int] = None) -> dict[str, Any]:
        state = self._read_json(self._state_path(session_id), root_fd=root_fd)
        if state is None:
            state = {}
        elif not (
            state.get("version", 1) == 1
            and not isinstance(state.get("version", 1), bool)
            and isinstance(state.get("session_id"), str)
            and state.get("session_id") == session_id
            and isinstance(state.get("epoch"), int)
            and not isinstance(state.get("epoch"), bool)
            and isinstance(state.get("committed_epoch"), int)
            and not isinstance(state.get("committed_epoch"), bool)
            and (state.get("owner") is None or isinstance(state.get("owner"), str))
            and isinstance(state.get("live_tip"), str)
            and (
                state.get("owner_pid") is None
                or (
                    isinstance(state.get("owner_pid"), int)
                    and not isinstance(state.get("owner_pid"), bool)
                )
            )
        ):
            raise RuntimeError("invalid compression spool state schema")
        return {
            "version": 1,
            "session_id": session_id,
            "epoch": int(state.get("epoch") or 0),
            "owner": state.get("owner"),
            "live_tip": state.get("live_tip") or session_id,
            "committed_epoch": int(state.get("committed_epoch") or 0),
            "owner_pid": state.get("owner_pid"),
            "lease_expires_ns": int(state.get("lease_expires_ns") or 0),
            "owner_process_identity": state.get("owner_process_identity"),
            "lease_started_monotonic_ns": int(
                state.get("lease_started_monotonic_ns") or 0
            ),
            "lease_duration_ns": int(state.get("lease_duration_ns") or 0),
        }

    @staticmethod
    def _process_liveness(pid: Any) -> tuple[str, Optional[str]]:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return "unknown", None
        if pid_int <= 0:
            return "unknown", None
        try:
            import psutil

            exists = psutil.pid_exists(pid_int)
            if exists is False:
                return "dead_or_reused", None
            if exists is not True:
                return "unknown", None
        except Exception:
            logging.getLogger(__name__).debug('DurableCompressionTurnSpool._process_liveness: optional operation failed', exc_info=True)
            return "unknown", None
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            try:
                import subprocess

                boot = subprocess.check_output(
                    ["sysctl", "-n", "kern.boottime"], text=True, timeout=5
                ).strip()
            except Exception:
                logging.getLogger(__name__).debug('DurableCompressionTurnSpool._process_liveness: optional operation failed', exc_info=True)
                return "unknown", None
        start = None
        try:
            fields = Path(f"/proc/{pid_int}/stat").read_text(
                encoding="utf-8"
            ).split()
            start = fields[21]
        except (OSError, IndexError):
            try:
                import subprocess

                start = subprocess.check_output(
                    ["ps", "-o", "lstart=", "-p", str(pid_int)], text=True, timeout=5
                ).strip()
            except Exception:
                logging.getLogger(__name__).debug('DurableCompressionTurnSpool._process_liveness: optional operation failed', exc_info=True)
                return "unknown", None
        if not boot or not start:
            return "unknown", None
        return "alive", f"{boot}:{pid_int}:{start}"

    @classmethod
    def _process_identity(cls, pid: Any) -> Optional[str]:
        status, identity = cls._process_liveness(pid)
        return identity if status == "alive" else None

    def _owner_liveness(self, state: dict[str, Any]) -> str:
        status, identity = self._process_liveness(state.get("owner_pid"))
        if status == "dead_or_reused":
            return status
        if status != "alive":
            return "unknown"
        stored = state.get("owner_process_identity")
        if not isinstance(stored, str) or not stored:
            return "unknown"
        return "alive" if identity == stored else "dead_or_reused"

    def begin_attempt(
        self,
        session_id: str,
        owner: str,
        *,
        allow_stale_owner_takeover: bool = False,
        lease_seconds: float = 300.0,
    ) -> Optional[int]:
        with self._lineage_locked(session_id) as root_fd:
            state = self._state(session_id, root_fd=root_fd)
            if state["owner"]:
                owner_liveness = self._owner_liveness(state)
                if not (
                    allow_stale_owner_takeover
                    and owner_liveness == "dead_or_reused"
                ):
                    return None
            own_status, own_identity = self._process_liveness(os.getpid())
            if own_status != "alive" or not own_identity:
                raise RuntimeError("compression spool process identity is unknown")
            state["epoch"] += 1
            state["owner"] = owner
            state["owner_pid"] = os.getpid()
            state["owner_process_identity"] = own_identity
            duration_ns = max(1, int(float(lease_seconds) * 1_000_000_000))
            state["lease_started_monotonic_ns"] = time.monotonic_ns()
            state["lease_duration_ns"] = duration_ns
            state["lease_expires_ns"] = time.time_ns() + max(
                1, duration_ns
            )
            self._write_json(self._state_path(session_id), state, root_fd=root_fd)
            return int(state["epoch"])

    def commit_attempt(
        self,
        session_id: str,
        owner: str,
        epoch: int,
        *,
        live_tip: str,
    ) -> bool:
        with self._lineage_locked(session_id) as root_fd:
            state = self._state(session_id, root_fd=root_fd)
            if state["owner"] != owner or int(state["epoch"]) != int(epoch):
                return False
            if self._owner_liveness(state) != "alive":
                return False
            if str(live_tip) != str(session_id):
                proposed_tip = self._resolve_live_tip(
                    str(live_tip), root_fd=root_fd
                )
                if proposed_tip == str(session_id):
                    return False
            if (
                time.monotonic_ns() - int(state["lease_started_monotonic_ns"] or 0)
                >= int(state["lease_duration_ns"] or 0)
            ):
                return False
            state["owner"] = None
            state["owner_pid"] = None
            state["owner_process_identity"] = None
            state["lease_expires_ns"] = 0
            state["committed_epoch"] = int(epoch)
            state["live_tip"] = str(live_tip)
            self._write_json(self._state_path(session_id), state, root_fd=root_fd)
            return True

    def refresh_attempt(
        self,
        session_id: str,
        owner: str,
        epoch: int,
        *,
        lease_seconds: float = 300.0,
    ) -> bool:
        with self._lineage_locked(session_id) as root_fd:
            state = self._state(session_id, root_fd=root_fd)
            if state["owner"] != owner or int(state["epoch"]) != int(epoch):
                return False
            duration_ns = max(1, int(float(lease_seconds) * 1_000_000_000))
            state["lease_started_monotonic_ns"] = time.monotonic_ns()
            state["lease_duration_ns"] = duration_ns
            state["lease_expires_ns"] = time.time_ns() + duration_ns
            self._write_json(self._state_path(session_id), state, root_fd=root_fd)
            return True

    def abort_attempt(self, session_id: str, owner: str, epoch: int) -> bool:
        with self._lineage_locked(session_id) as root_fd:
            state = self._state(session_id, root_fd=root_fd)
            if state["owner"] != owner or int(state["epoch"]) != int(epoch):
                return False
            state["owner"] = None
            state["owner_pid"] = None
            state["owner_process_identity"] = None
            state["lease_expires_ns"] = 0
            self._write_json(self._state_path(session_id), state, root_fd=root_fd)
            return True

    def reconcile_canonical_tip(
        self,
        session_id: str,
        live_tip: str,
        *,
        expected_owner: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> bool:
        """Record a SessionDB-proven child after a partial spool commit."""
        if not live_tip:
            return False
        with self._lineage_locked(session_id) as root_fd:
            state = self._state(session_id, root_fd=root_fd)
            if str(live_tip) != str(session_id):
                proposed_tip = self._resolve_live_tip(
                    str(live_tip), root_fd=root_fd
                )
                if proposed_tip == str(session_id):
                    return False
            caller_still_fenced = (
                expected_owner is not None
                and expected_epoch is not None
                and state["owner"] == expected_owner
                and int(state["epoch"]) == int(expected_epoch)
                and self._owner_liveness(state) == "alive"
            )
            if state["live_tip"] == str(live_tip) and not state["owner"]:
                self._resync_file(self._state_path(session_id), root_fd=root_fd)
                return True
            if not caller_still_fenced:
                if state["owner"] and self._owner_liveness(state) != "dead_or_reused":
                    return False
                state["epoch"] = int(state["epoch"]) + 1
            state["committed_epoch"] = int(state["epoch"])
            state["owner"] = None
            state["owner_pid"] = None
            state["owner_process_identity"] = None
            state["lease_expires_ns"] = 0
            state["lease_started_monotonic_ns"] = 0
            state["lease_duration_ns"] = 0
            state["live_tip"] = str(live_tip)
            self._write_json(self._state_path(session_id), state, root_fd=root_fd)
            return True

    def _resolve_live_tip(self, session_id: str, *, root_fd: int) -> str:
        current = str(session_id)
        visited: set[str] = set()
        while current not in visited:
            visited.add(current)
            tip = str(self._state(current, root_fd=root_fd)["live_tip"] or current)
            if tip == current:
                return current
            current = tip
        raise RuntimeError(
            f"compression spool live-tip cycle detected for session: {session_id}"
        )

    def resolve_live_tip(self, session_id: str) -> str:
        with self._locked("global") as root_fd:
            return self._resolve_live_tip(session_id, root_fd=root_fd)

    def _iter_record_files(self, *, root_fd: int):
        return [
            self.root / name
            for name in os.listdir(root_fd)
            if name.endswith(".json")
            and not name.startswith("state-")
            and name != "global-state.json"
        ]

    def _record_rows(self, *, root_fd: int) -> list[tuple[Path, dict[str, Any]]]:
        rows = []
        for path in self._iter_record_files(root_fd=root_fd):
            payload = self._read_json(path, root_fd=root_fd)
            if payload is None:
                continue
            payload.setdefault("session_id", payload.get("origin_session"))
            payload.setdefault("origin_session", payload.get("session_id"))
            payload.setdefault("client_turn_id", path.stem)
            payload.setdefault("created_ns", 0)
            payload.setdefault("sequence", int(payload.get("created_ns") or 0))
            payload.setdefault("status", "pending")
            valid = (
                isinstance(payload.get("origin_session"), str)
                and bool(payload.get("origin_session"))
                and isinstance(payload.get("session_id"), str)
                and bool(payload.get("session_id"))
                and payload.get("session_id") == payload.get("origin_session")
                and isinstance(payload.get("client_turn_id"), str)
                and bool(payload.get("client_turn_id"))
                and isinstance(payload.get("message"), dict)
                and isinstance(payload["message"].get("role"), str)
                and "content" in payload["message"]
                and isinstance(payload.get("created_ns"), int)
                and not isinstance(payload.get("created_ns"), bool)
                and isinstance(payload.get("sequence"), int)
                and not isinstance(payload.get("sequence"), bool)
                and payload.get("status") in {"pending", "drained"}
                and (
                    "version" not in payload
                    or (
                        payload.get("version") == 1
                        and not isinstance(payload.get("version"), bool)
                        and isinstance(payload.get("token_count"), int)
                        and not isinstance(payload.get("token_count"), bool)
                    )
                )
            )
            if not valid:
                raise RuntimeError(f"invalid compression spool record schema: {path.name}")
            rows.append((path, payload))
        rows.sort(key=lambda item: (int(item[1]["sequence"]), int(item[1]["created_ns"])))
        return rows

    def enqueue(
        self,
        session_id: str,
        message: dict[str, Any],
        *,
        token_count: int,
        client_turn_id: Optional[str] = None,
    ) -> dict[str, Any]:
        turn_id = str(client_turn_id or uuid.uuid4().hex)
        with self._locked("global") as root_fd:
            # A durable ACK promises that this session's lineage can later be
            # resolved and drained. Validate its state before touching global
            # sequence metadata or creating/revalidating a record. State for
            # unrelated sessions is intentionally not consulted.
            self._resolve_live_tip(str(session_id), root_fd=root_fd)
            for path, payload in self._record_rows(root_fd=root_fd):
                if (
                    payload.get("origin_session") == session_id
                    and payload.get("client_turn_id") == turn_id
                ):
                    self._resync_file(path, root_fd=root_fd)
                    self._assert_canonical_root(root_fd, path.name)
                    return {"accepted": True, "durable": True, "turn_id": turn_id, "path": path}
            global_path = self.root / "global-state.json"
            global_state = self._read_json(global_path, root_fd=root_fd)
            if global_state is None:
                global_state = {"version": 1, "sequence": 0}
            elif not (
                global_state.get("version", 1) == 1
                and not isinstance(global_state.get("version", 1), bool)
                and isinstance(global_state.get("sequence"), int)
                and not isinstance(global_state.get("sequence"), bool)
            ):
                raise RuntimeError("invalid compression spool global schema")
            else:
                self._resync_file(global_path, root_fd=root_fd)
            sequence = int(global_state.get("sequence") or 0) + 1
            global_state["sequence"] = sequence
            global_state["version"] = 1
            self._write_json(global_path, global_state, root_fd=root_fd)
            created_ns = time.time_ns()
            record_path = self.root / f"turn-{sequence:020d}-{uuid.uuid4().hex}.json"
            payload = {
                "version": 1,
                "origin_session": str(session_id),
                "session_id": str(session_id),
                "client_turn_id": turn_id,
                "message": copy.deepcopy(message),
                "token_count": int(token_count),
                "sequence": sequence,
                "created_ns": created_ns,
                "status": "pending",
            }
            self._write_json(record_path, payload, root_fd=root_fd)
            self._assert_canonical_root(root_fd, record_path.name)
            return {
                "accepted": True,
                "durable": True,
                "turn_id": turn_id,
                "path": record_path,
            }

    def records(self, session_id: str) -> list[dict[str, Any]]:
        with self._locked("global") as root_fd:
            target_tip = self._resolve_live_tip(session_id, root_fd=root_fd)
            return [
                copy.deepcopy(payload)
                for _path, payload in self._record_rows(root_fd=root_fd)
                if payload.get("origin_session")
                and self._resolve_live_tip(
                    str(payload["origin_session"]), root_fd=root_fd
                ) == target_tip
            ]

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        return [row for row in self.records(session_id) if row.get("status") != "drained"]

    def drain(self, session_id: str, consumer: Callable[[dict[str, Any], str], None]) -> list[str]:
        drained: list[str] = []
        with self._locked("global") as root_fd:
            live_tip = self._resolve_live_tip(session_id, root_fd=root_fd)
            for path, payload in self._record_rows(root_fd=root_fd):
                origin = payload.get("origin_session")
                if (
                    not origin
                    or self._resolve_live_tip(str(origin), root_fd=root_fd) != live_tip
                    or payload.get("status") == "drained"
                ):
                    continue
                consumer(copy.deepcopy(payload), live_tip)
                payload["status"] = "drained"
                payload["drained_tip"] = live_tip
                payload["drained_ns"] = time.time_ns()
                self._write_json(path, payload, root_fd=root_fd)
                drained.append(str(payload["client_turn_id"]))
        return drained


def compression_turn_spool() -> DurableCompressionTurnSpool:
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    return DurableCompressionTurnSpool(home / "compression_turn_spool")


def ensure_compression_turn_identity(message: dict[str, Any]) -> str:
    """Stamp one stable internal identity before transcript persistence."""
    metadata = message.get("display_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        message["display_metadata"] = metadata
    turn_id = str(
        message.get("client_turn_id")
        or message.get("platform_message_id")
        or message.get("message_id")
        or message.get("_compression_spool_turn_id")
        or metadata.get("_compression_turn_id")
        or f"compression-turn:{uuid.uuid4().hex}"
    )
    message["client_turn_id"] = turn_id
    message["_compression_spool_turn_id"] = turn_id
    metadata["_compression_turn_id"] = turn_id
    return turn_id


def spool_deferred_compression_turn(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    token_count: int,
) -> Optional[dict[str, Any]]:
    """Durably accept the newest user turn on compression-lock contention."""
    source = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        None,
    )
    if source is None:
        return None
    message = copy.deepcopy(source)
    message.pop("_db_persisted", None)
    turn_id = ensure_compression_turn_identity(message)
    return compression_turn_spool().enqueue(
        session_id,
        message,
        token_count=int(token_count),
        client_turn_id=turn_id,
    )


def drain_compression_turns_into_agent(
    agent: Any,
    origin_session_id: str,
    live_messages: list[dict[str, Any]],
) -> list[str]:
    """Drain queued turns to the transitive live tip with durable dedupe."""
    session_db = getattr(agent, "_session_db", None)
    if session_db is None:
        return []
    spool = compression_turn_spool()

    def _consume(record: dict[str, Any], live_tip: str) -> None:
        message = copy.deepcopy(record["message"])
        turn_id = str(record["client_turn_id"])
        spool_dedupe_id = f"compression-spool:{turn_id}"
        original_ids = {
            str(value)
            for value in (
                message.get("platform_message_id"),
                message.get("message_id"),
                message.get("client_turn_id"),
                (message.get("display_metadata") or {}).get(
                    "_compression_turn_id"
                ) if isinstance(message.get("display_metadata"), dict) else None,
            )
            if value not in (None, "")
        }
        dedupe_ids = {spool_dedupe_id, *original_ids}
        already_live = False
        for existing in live_messages:
            if not isinstance(existing, dict):
                continue
            existing_ids = {
                str(value)
                for value in (
                    existing.get("_compression_spool_turn_id"),
                    existing.get("platform_message_id"),
                    existing.get("message_id"),
                    existing.get("client_turn_id"),
                    (existing.get("display_metadata") or {}).get(
                        "_compression_turn_id"
                    ) if isinstance(existing.get("display_metadata"), dict) else None,
                )
                if value not in (None, "")
            }
            if turn_id in existing_ids or bool(original_ids & existing_ids):
                already_live = True
                break
        has_id = getattr(session_db, "has_platform_message_id", None)
        already_durable = bool(
            callable(has_id)
            and any(has_id(live_tip, candidate) for candidate in dedupe_ids)
        ) or already_live
        if not already_durable:
            persisted_id = next(iter(sorted(original_ids)), spool_dedupe_id)
            session_db.append_message(
                live_tip,
                str(message.get("role") or "user"),
                message.get("content"),
                platform_message_id=persisted_id,
                timestamp=message.get("timestamp"),
            )
        if not already_live:
            message["_compression_spool_turn_id"] = turn_id
            message["_db_persisted"] = True
            live_messages.append(message)

    return spool.drain(origin_session_id, _consume)



def commit_compression_turn_spool(agent, lease, compressed):
    """Publish the durable continuation only while its lease still owns the epoch."""
    if lease.spool_epoch is None or not lease.holder:
        return
    try:
        durable_holder = (
            lease.db.get_compression_lock_holder(lease.sid)
            if lease.db is not None
            else None
        )
        if durable_holder != lease.holder:
            raise RuntimeError("compression spool owner lost durable lease")
        if not compression_turn_spool().refresh_attempt(
            lease.sid,
            lease.holder,
            lease.spool_epoch,
            lease_seconds=lease.ttl,
        ):
            raise RuntimeError("compression spool epoch became stale")
        lease.spool_committed = compression_turn_spool().commit_attempt(
            lease.sid,
            lease.holder,
            lease.spool_epoch,
            live_tip=agent.session_id or lease.sid,
        )
        if lease.spool_committed:
            drain_compression_turns_into_agent(
                agent,
                lease.sid,
                compressed,
            )
    except Exception:
        logger.warning(
            "compression turn spool commit/drain failed for %s",
            lease.sid,
            exc_info=True,
        )
        try:
            _canonical_tip = (
                lease.db.get_compression_tip(lease.sid)
                if lease.db is not None
                else None
            )
            if compression_turn_spool().reconcile_canonical_tip(
                lease.sid,
                str(_canonical_tip or ""),
                expected_owner=lease.holder,
                expected_epoch=lease.spool_epoch,
            ):
                drain_compression_turns_into_agent(
                    agent, lease.sid, compressed
                )
        except Exception:
            logger.warning(
                "compression canonical tip reconciliation failed for %s",
                lease.sid,
                exc_info=True,
            )



def register_compression_turn_spool(agent, lease) -> bool:
    """Bind the spool epoch to the acquired lease before any summary work."""
    # Serialize registration with watchdog cancellation. A released worker must
    # never create an epoch after its successor has acquired the durable lock.
    with lease._release_guard:
        if lease._released:
            return False
        if lease.holder is None or not lease.sid:
            return True
        try:
            lease.spool_epoch = compression_turn_spool().begin_attempt(
                lease.sid, lease.holder, allow_stale_owner_takeover=True)
        except Exception:
            logger.warning("compression spool registration failed", exc_info=True)
        if lease.spool_epoch is not None:
            return True
    agent._compression_skipped_due_to_lock = "spool_owner_unavailable"
    lease.release()
    return False
