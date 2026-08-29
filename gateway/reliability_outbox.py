"""Local, default-off client for the Kindra reliability outbox contract.

The control-plane dispatcher owns delivery. Hermes only persists an owner-only
payload and the metadata row that lets the dispatcher reconcile it. In
``off`` mode this module has no filesystem side effects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_events(
  event_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  producer TEXT NOT NULL,
  work_id TEXT,
  event_type TEXT NOT NULL,
  milestone TEXT NOT NULL,
  version TEXT NOT NULL,
  destination TEXT NOT NULL,
  payload_ref TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'pending','sending','accepted','delivered','unknown',
    'reconciled','retry_wait','dead_letter')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at INTEGER,
  accepted_at INTEGER,
  delivered_at INTEGER,
  next_attempt_at INTEGER,
  last_error_code TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due
  ON outbox_events(state,next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS outbox_attempts(
  event_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  adapter_receipt TEXT,
  result TEXT NOT NULL,
  observed_at INTEGER NOT NULL,
  PRIMARY KEY(event_id,attempt),
  FOREIGN KEY(event_id) REFERENCES outbox_events(event_id)
);
"""

_VALID_MODES = frozenset({"off", "shadow"})


@dataclass(frozen=True)
class OutboxSettings:
    mode: str
    db_path: Path
    payload_dir: Path


@dataclass(frozen=True)
class OutboxReceipt:
    event_id: str
    idempotency_key: str
    payload_ref: str
    inserted: bool


class OutboxConflict(RuntimeError):
    """The same idempotency key was reused for different payload bytes."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scoped_path(value: Any, *, default: Path, hermes_home: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        return default
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else hermes_home / candidate


def resolve_outbox_settings(
    config: Optional[Mapping[str, Any]] = None,
    *,
    hermes_home: Optional[Path] = None,
) -> OutboxSettings:
    """Resolve ``gateway.reliability.outbox`` with a fail-safe off default."""
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            logger.warning("Could not load reliability outbox config; using off")
            config = {}
    gateway = _mapping(_mapping(config).get("gateway"))
    reliability = _mapping(gateway.get("reliability"))
    raw = _mapping(reliability.get("outbox"))
    candidate_mode = str(raw.get("mode", "off")).strip().lower()
    mode = candidate_mode if candidate_mode in _VALID_MODES else "off"
    if candidate_mode not in _VALID_MODES:
        logger.warning(
            "Ignoring gateway.reliability.outbox.mode=%r; expected off or shadow",
            candidate_mode,
        )
    state_dir = home / "state" / "reliability"
    return OutboxSettings(
        mode=mode,
        db_path=_scoped_path(
            raw.get("db_path"),
            default=state_dir / "outbox.sqlite3",
            hermes_home=home,
        ),
        payload_dir=_scoped_path(
            raw.get("payload_dir"),
            default=state_dir / "payloads",
            hermes_home=home,
        ),
    )


class ReliabilityOutbox:
    """SQLite/payload-file writer compatible with the POS outbox dispatcher."""

    def __init__(self, settings: OutboxSettings):
        self.settings = settings
        self._lock = threading.RLock()

    @classmethod
    def from_config(
        cls,
        config: Optional[Mapping[str, Any]] = None,
        *,
        hermes_home: Optional[Path] = None,
    ) -> "ReliabilityOutbox":
        return cls(resolve_outbox_settings(config, hermes_home=hermes_home))

    @classmethod
    def disabled(cls, *, hermes_home: Optional[Path] = None) -> "ReliabilityOutbox":
        return cls(resolve_outbox_settings({}, hermes_home=hermes_home))

    @property
    def mode(self) -> str:
        return self.settings.mode

    def _connect(self) -> sqlite3.Connection:
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.payload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.settings.payload_dir, 0o700)
        conn = sqlite3.connect(self.settings.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_OUTBOX_SCHEMA)
        os.chmod(self.settings.db_path, 0o600)
        return conn

    @staticmethod
    def canonical_key(
        *,
        producer: str,
        work_id: str,
        event_type: str,
        milestone: str,
        version: str,
        destination: str,
    ) -> str:
        material = "\0".join(
            (producer, work_id, event_type, milestone, version, destination)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def enqueue(
        self,
        *,
        payload: bytes,
        producer: str,
        work_id: str,
        event_type: str,
        milestone: str,
        version: str,
        destination: str,
    ) -> OutboxReceipt:
        """Persist one event, deduplicating the canonical metadata identity."""
        if self.mode != "shadow":
            raise RuntimeError("reliability outbox enqueue requires shadow mode")
        fields = {
            "producer": producer,
            "event_type": event_type,
            "milestone": milestone,
            "version": version,
            "destination": destination,
        }
        if any(not isinstance(value, str) or not value for value in fields.values()):
            raise ValueError("outbox identity fields must be non-empty strings")
        if not isinstance(payload, bytes):
            raise TypeError("outbox payload must be bytes")

        key = self.canonical_key(
            producer=producer,
            work_id=work_id or "",
            event_type=event_type,
            milestone=milestone,
            version=version,
            destination=destination,
        )
        payload_sha = hashlib.sha256(payload).hexdigest()
        now = time.time_ns() // 1_000_000
        event_id = uuid.uuid4().hex

        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT event_id, payload_ref, payload_sha256 FROM outbox_events "
                "WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing[2] != payload_sha:
                    raise OutboxConflict(
                        "outbox idempotency key already owns different payload bytes"
                    )
                return OutboxReceipt(existing[0], key, existing[1], False)

            payload_path = self.settings.payload_dir / f"{event_id}.json"
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{event_id}.", dir=self.settings.payload_dir
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, payload_path)
                os.chmod(payload_path, 0o600)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

            try:
                conn.execute(
                    """INSERT INTO outbox_events(
                         event_id,idempotency_key,producer,work_id,event_type,
                         milestone,version,destination,payload_ref,payload_sha256,
                         state,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (
                        event_id,
                        key,
                        producer,
                        work_id or None,
                        event_type,
                        milestone,
                        version,
                        destination,
                        str(payload_path),
                        payload_sha,
                        now,
                        now,
                    ),
                )
            except Exception:
                try:
                    payload_path.unlink()
                except OSError:
                    pass
                raise
            return OutboxReceipt(event_id, key, str(payload_path), True)

    def enqueue_json(self, payload: Mapping[str, Any], **identity: str) -> OutboxReceipt:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.enqueue(payload=encoded, **identity)
