"""Opt-in, profile-local automation inbox; never writes into a human chat."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid

import yaml

INBOX_TITLE = "Atualizações automáticas"
INBOX_SOURCE = "automation_inbox"


def enabled(home: Path) -> bool:
    """Read the destination profile on every delivery (no restart needed)."""
    path = home / "config.yaml"
    if not path.exists():
        return False
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (config.get("notifications") or {}).get("isolated_inbox") is True


def append(home: Path, content: str, *, delivery_id: str, source: str,
           origin_session: str = "") -> dict:
    """Persist once under a separate registry name, including on retry.

    The normal DB turn/compression guards also protect someone who opens the
    inbox and starts talking there. Failure never falls back to the Bot Chat.
    """
    from hermes_cli.active_sessions import _FileLock
    from hermes_state import SessionDB

    home = Path(home)
    with _FileLock(home / "notifications" / ".inbox.lock"):
        db = SessionDB(db_path=home / "state.db")
        try:
            row = db.get_session_by_title(INBOX_TITLE)
            if row and row.get("source") != INBOX_SOURCE:
                raise ValueError("notification inbox title belongs to a human conversation")
            if not row:
                sid = "inbox_" + uuid.uuid5(uuid.NAMESPACE_URL, str(home.resolve())).hex
                existing = db.get_session(sid)
                if existing and existing.get("source") != INBOX_SOURCE:
                    raise ValueError("notification inbox identity conflict")
                if not existing:
                    db.create_session(sid, INBOX_SOURCE)
                if not db.set_session_title(sid, INBOX_TITLE):
                    raise RuntimeError("notification inbox could not be registered")
                row = db.get_session(sid)
            sid = db.get_compression_tip(row["id"]) or row["id"]
            holder = f"pid={os.getpid()}:inbox={uuid.uuid4()}"
            if not db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=30):
                raise RuntimeError("notification inbox is busy")
            try:
                if not any(db.has_platform_message_id(segment, delivery_id)
                           for segment in db.get_compression_lineage(sid)):
                    db.append_message(
                        sid, "user", content, platform_message_id=delivery_id,
                        display_kind="internal_notification",
                        display_metadata={"source": source, "passive": True,
                                          "origin_session": origin_session},
                        turn_lease_holder=holder,
                    )
                db.touch_session_activity(sid, description="Atualização automática")
            finally:
                db.release_session_turn_lease(sid, holder)
            return {"session_id": sid, "delivery_id": delivery_id,
                    "sha256": hashlib.sha256(content.encode()).hexdigest()}
        finally:
            db.close()
