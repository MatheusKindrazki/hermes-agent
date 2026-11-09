"""Extracted bot mode dm ledger helpers."""
from __future__ import annotations
import contextlib
from pathlib import Path
from typing import Any, Optional, Mapping


@contextlib.contextmanager
def _delivery_ledger_lock(idempotency_key: str):
    """Cross-process lock whose ownership dies with the process, not the file."""
    from tools.bot_mode_dm import (
        _delivery_ledger_path,
        fcntl,
        logging,
        os,
        stat,
        time,
    )
    path = _delivery_ledger_path(idempotency_key).with_suffix(".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("delivery_lock_unsafe")
        if fcntl is None:
            import msvcrt  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("delivery idempotency lock busy")
                    time.sleep(0.01)
        yield
    finally:
        try:
            if fcntl is None:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[name-defined]  # pragma: no cover
                except Exception:
                    logging.getLogger(__name__).debug('_delivery_ledger_lock: optional operation failed', exc_info=True)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)



def _read_delivery_ledger(path: Path, idempotency_key: str) -> Optional[dict[str, Any]]:
    from tools.bot_mode_dm import (
        json,
        os,
        stat,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("delivery_ledger_unsafe")
        try:
            with os.fdopen(fd, "r", encoding="utf-8-sig") as stream:
                fd = -1
                document = json.load(stream)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("delivery_ledger_unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(document, dict) or document.get("idempotency_key") != idempotency_key:
        raise RuntimeError("delivery_ledger_rebind")
    if not isinstance(document.get("delivery_id"), str) or not document["delivery_id"]:
        raise RuntimeError("delivery_ledger_invalid")
    return document



def _write_delivery_receipt(
    dm_file: str, *, origin_session_id: str, origin_reason: str, route_reason: str,
    label: str, idempotency_key: str, turn_identity: Optional[Mapping[str, Any]] = None,
    fence: Optional[Mapping[str, Any]] = None,
    observation: Optional[dict] = None, ledger_locked: bool = False, enforced_effect: bool = False,
    author: Optional[dict] = None, profile_home: Path | None = None,
) -> dict[str, Any]:
    """Persist the accepted-state receipt before spawning a child process.

    A terminal PID only proves that the Desktop accepted a spawn request.  The
    sidecar is intentionally content-free and gives retries a stable delivery
    identity tied to the originating Bot Chat session.  The child updates this
    same record to a terminal state in :func:`_run_delivery`.
    """
    from tools.bot_mode_dm import (
        DELIVERY_ENVELOPE_SCHEMA,
        Path,
        _atomic_json,
        _canonical_sha256,
        _delivery_envelope_path,
        _delivery_ledger_lock,
        _delivery_ledger_path,
        _read_delivery_ledger,
        contextlib,
        time,
        uuid,
    )
    ledger = _delivery_ledger_path(idempotency_key)
    with (contextlib.nullcontext() if ledger_locked else _delivery_ledger_lock(idempotency_key)):
        existing = _read_delivery_ledger(ledger, idempotency_key)
        if isinstance(existing, dict) and existing.get("idempotency_key") == idempotency_key:
            existing["duplicate"] = True
            _atomic_json(Path(dm_file + ".receipt.json"), existing)
            if isinstance(existing.get("turn_identity"), dict):
                _atomic_json(_delivery_envelope_path(dm_file), {
                    "schema": DELIVERY_ENVELOPE_SCHEMA,
                    "delivery_id": existing["delivery_id"],
                    "request_key": existing["turn_identity"]["request_key"],
                    "turn_identity_sha256": _canonical_sha256(existing["turn_identity"]),
                    "accepted_at": int(existing["accepted_at"]),
                })
            return existing
        delivery_id = str(uuid.uuid4())
        record = {
            "schema": "hermes.delivery/v1", "delivery_id": delivery_id,
            "origin_session_id": str(origin_session_id or ""),
            "origin_reason": origin_reason, "route_reason": route_reason, "target": label,
            "idempotency_key": idempotency_key, "state": "accepted",
            "accepted_at": int(time.time()),
        }
        if author:
            record["turn_author"] = dict(author)
        if profile_home is not None:
            record["profile_home"] = str(profile_home)
        if observation is not None or enforced_effect:
            from agent.durable_admission import new_release_event_id
            record["release_event_id"] = new_release_event_id()
        if observation is not None:
            record["observation"] = observation
        if enforced_effect:
            record["enforced_effect"] = True
        if turn_identity is not None:
            record["turn_identity"] = dict(turn_identity)
            record["fence"] = dict(fence or {})
        path = Path(dm_file + ".receipt.json")
        _atomic_json(path, record)
        _atomic_json(ledger, record)
        if turn_identity is not None:
            _atomic_json(_delivery_envelope_path(dm_file), {
                "schema": DELIVERY_ENVELOPE_SCHEMA,
                "delivery_id": delivery_id,
                "request_key": turn_identity["request_key"],
                "turn_identity_sha256": _canonical_sha256(turn_identity),
                "accepted_at": record["accepted_at"],
            })
        return record

