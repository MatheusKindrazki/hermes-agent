"""Durable idempotency ledger for the public ``POST /v1/runs`` receiver."""

from __future__ import annotations

import os
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_request_idempotency(
  idempotency_key TEXT PRIMARY KEY,
  request_sha256 TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'claimed',
  receipt_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


class RunIdempotencyConflict(RuntimeError):
    """One client key attempted to claim different request bytes."""


@dataclass(frozen=True)
class RunIdempotencyClaim:
    run_id: str
    inserted: bool


@dataclass(frozen=True)
class RunIdempotencyRecord:
    run_id: str
    state: str
    receipt: dict


class RunIdempotencyLedger:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else (
            get_hermes_home()
            / "state"
            / "reliability"
            / "run-idempotency.sqlite3"
        )
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.db_path.parent, 0o700)
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(_SCHEMA)
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(run_request_idempotency)"
            ).fetchall()
        }
        if "state" not in columns:
            connection.execute(
                "ALTER TABLE run_request_idempotency "
                "ADD COLUMN state TEXT NOT NULL DEFAULT 'claimed'"
            )
        if "receipt_json" not in columns:
            connection.execute(
                "ALTER TABLE run_request_idempotency "
                "ADD COLUMN receipt_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._ensure_owner_only_sqlite_modes()
        return connection

    def _ensure_owner_only_sqlite_modes(self) -> None:
        """Keep the ledger and live SQLite sidecars private to their owner."""
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                continue

    @staticmethod
    def _validate_existing(
        row: tuple[str, str, str],
        *,
        request_sha256: str,
        payload_sha256: str,
    ) -> str:
        if row[0] != request_sha256 or row[1] != payload_sha256:
            raise RunIdempotencyConflict(
                "run idempotency key already owns a different request payload"
            )
        return row[2]

    def lookup(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        payload_sha256: str,
    ) -> Optional[str]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT request_sha256,payload_sha256,run_id "
                "FROM run_request_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            return self._validate_existing(
                row,
                request_sha256=request_sha256,
                payload_sha256=payload_sha256,
            )

    @staticmethod
    def _record(row: tuple[str, str, str]) -> RunIdempotencyRecord:
        try:
            receipt = json.loads(row[2])
        except (TypeError, ValueError):
            receipt = {}
        return RunIdempotencyRecord(
            run_id=str(row[0]),
            state=str(row[1] or "claimed"),
            receipt=receipt if isinstance(receipt, dict) else {},
        )

    def lookup_record(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        payload_sha256: str,
    ) -> Optional[RunIdempotencyRecord]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT request_sha256,payload_sha256,run_id,state,receipt_json "
                "FROM run_request_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            self._validate_existing(
                (row[0], row[1], row[2]),
                request_sha256=request_sha256,
                payload_sha256=payload_sha256,
            )
            return self._record((row[2], row[3], row[4]))

    def lookup_run(self, run_id: str) -> Optional[RunIdempotencyRecord]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT run_id,state,receipt_json "
                "FROM run_request_idempotency WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return self._record(row) if row is not None else None

    def update_state(self, run_id: str, state: str, receipt: dict) -> None:
        now = time.time_ns() // 1_000_000
        encoded = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE run_request_idempotency "
                "SET state = ?,receipt_json = ?,updated_at = ? WHERE run_id = ?",
                (state, encoded, now, run_id),
            )

    def attach(self, run_id: str, receipt: dict) -> None:
        self.update_state(run_id, "attached", receipt)

    def claim(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        payload_sha256: str,
        proposed_run_id: str,
    ) -> RunIdempotencyClaim:
        now = time.time_ns() // 1_000_000
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_owner_only_sqlite_modes()
            row = connection.execute(
                "SELECT request_sha256,payload_sha256,run_id "
                "FROM run_request_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return RunIdempotencyClaim(
                    run_id=self._validate_existing(
                        row,
                        request_sha256=request_sha256,
                        payload_sha256=payload_sha256,
                    ),
                    inserted=False,
                )
            connection.execute(
                """INSERT INTO run_request_idempotency(
                     idempotency_key,request_sha256,payload_sha256,run_id,
                     created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    idempotency_key,
                    request_sha256,
                    payload_sha256,
                    proposed_run_id,
                    now,
                    now,
                ),
            )
            return RunIdempotencyClaim(run_id=proposed_run_id, inserted=True)
