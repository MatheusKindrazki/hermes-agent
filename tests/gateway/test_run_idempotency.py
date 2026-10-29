"""Owner-only durability invariants for the public run idempotency ledger."""

import os
import stat

from gateway.run_idempotency import RunIdempotencyLedger


def test_live_sqlite_database_and_sidecars_are_owner_only_under_umask_zero(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "run-idempotency.sqlite3"
    ledger = RunIdempotencyLedger(db_path)
    observed: dict[str, int] = {}
    original = ledger._ensure_owner_only_sqlite_modes

    def capture_live_modes():
        original()
        for path in (db_path, db_path.with_name(f"{db_path.name}-wal"), db_path.with_name(f"{db_path.name}-shm")):
            if path.exists():
                observed[path.name] = stat.S_IMODE(path.stat().st_mode)

    monkeypatch.setattr(ledger, "_ensure_owner_only_sqlite_modes", capture_live_modes)
    previous_umask = os.umask(0)
    try:
        ledger.claim(
            idempotency_key="turn:owner-only:1",
            request_sha256="a" * 64,
            payload_sha256="b" * 64,
            proposed_run_id="run_owner_only",
        )
    finally:
        os.umask(previous_umask)

    assert observed == {
        "run-idempotency.sqlite3": 0o600,
        "run-idempotency.sqlite3-shm": 0o600,
        "run-idempotency.sqlite3-wal": 0o600,
    }
