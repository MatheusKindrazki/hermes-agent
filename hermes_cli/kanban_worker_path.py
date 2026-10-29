"""Immutable, hash-verified shell environment images for Kanban workers."""
from __future__ import annotations
import hashlib
import os
import re
import secrets
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# Capture before custom launchers replace Popen; verification always executes real bash.
_REAL_SUBPROCESS_POPEN = subprocess.Popen

_WORKER_PATH_SOURCE_SCRIPT = r'''
source "$1" || {
  rc=$?
  printf '%s\n' worker_path_source_failed >&2
  exit "$rc"
}
'''

_WORKER_PATH_EXEC_SCRIPT = _WORKER_PATH_SOURCE_SCRIPT + r'''
shift
exec "$@"
'''


@dataclass(frozen=True)
class _WorkerPathSnapshot:
    approved_bytes: bytes
    original_path: str
    sha256: str

    def open_source_image(self):
        """Return an unlinked, read-only FD containing approved bytes."""
        write_fd, temp_path = tempfile.mkstemp(prefix="hermes-worker-path-")
        try:
            view = memoryview(self.approved_bytes)
            while view:
                written = os.write(write_fd, view)
                view = view[written:]
            os.fsync(write_fd)
        finally:
            os.close(write_fd)
        try:
            read_fd = os.open(temp_path, os.O_RDONLY)
        finally:
            os.unlink(temp_path)
        return os.fdopen(read_fd, "rb")

    # Tests and callers can use one stable spelling without knowing the FD
    # until an immutable per-process image is opened.
    @staticmethod
    def source_path_for(fd: int) -> str:
        return f"/dev/fd/{fd}"

    @property
    def fd(self) -> int:
        image = getattr(self, "_test_image", None)
        if image is None:
            image = self.open_source_image()
            object.__setattr__(self, "_test_image", image)
        return image.fileno()

    @property
    def source_path(self) -> str:
        return self.source_path_for(self.fd)

    def close(self) -> None:
        image = getattr(self, "_test_image", None)
        if image is not None:
            image.close()
            object.__setattr__(self, "_test_image", None)


def _probe_worker_path_snapshot(
    snapshot: _WorkerPathSnapshot,
    env: Mapping[str, str],
) -> None:
    image = snapshot.open_source_image()
    try:
        probe = _REAL_SUBPROCESS_POPEN(
            [
                "/bin/bash",
                "-c",
                _WORKER_PATH_SOURCE_SCRIPT,
                "hermes-worker-path-probe",
                snapshot.source_path_for(image.fileno()),
            ],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(image.fileno(),),
        )
        try:
            probe.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            probe.kill()
            probe.communicate(timeout=5)
            raise RuntimeError("worker_path_probe_timeout") from exc
    finally:
        image.close()
    if probe.returncode != 0:
        raise RuntimeError("worker_path_source_failed")


def _snapshot_worker_path_lib(env: Mapping[str, str]) -> _WorkerPathSnapshot:
    """Read once, hash once, then source only immutable approved bytes.

    The configured pathname is opened exactly once with ``O_NOFOLLOW`` when
    available. Probe and launcher receive separate unlinked read-only images
    of the same immutable ``bytes`` object, so replacing or rewriting the
    configured pathname after approval cannot change what gets sourced.
    """
    lib_value = (env.get("HERMES_WORKER_PATH_LIB") or "").strip()
    if not lib_value:
        raise RuntimeError("worker_path_lib_missing")
    lib_path = Path(lib_value)
    if not lib_path.is_absolute():
        raise RuntimeError("worker_path_lib_invalid")

    expected = (env.get("K5_WORKER_PATH_SHA256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("worker_path_hash_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(str(lib_path), flags)
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise RuntimeError("worker_path_lib_invalid")
            chunks = []
            while True:
                chunk = os.read(source_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            approved_bytes = b"".join(chunks)
        finally:
            os.close(source_fd)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("worker_path_hash_unavailable") from exc
    actual = hashlib.sha256(approved_bytes).hexdigest()
    if not secrets.compare_digest(actual, expected):
        raise RuntimeError("worker_path_hash_mismatch")

    snapshot = _WorkerPathSnapshot(
        approved_bytes=approved_bytes,
        original_path=str(lib_path),
        sha256=expected,
    )
    _probe_worker_path_snapshot(snapshot, env)
    return snapshot


def _validate_worker_path_lib(env: Mapping[str, str]) -> tuple[str, str]:
    """Backward-compatible validation helper backed by immutable snapshot."""
    snapshot = _snapshot_worker_path_lib(env)
    return snapshot.original_path, snapshot.sha256
