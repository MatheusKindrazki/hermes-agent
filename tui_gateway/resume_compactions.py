"""How many context compactions a stored session has had, for ``session.resume``.

The voice client rotates its Hermes session after three compactions (RFC-001 V1). It cannot count them
itself: the resume ``messages`` projection drops or unwraps every compaction carrier
(``project_compaction_message_for_display``), and ``info.usage.compressions`` is the in-process counter of
the agent just built, so a cold resume reads 0. The carriers stay in ``messages``, so the stored transcript is
where the count lives.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The opening of ``agent.context_compressor.SUMMARY_PREFIX`` and ``LEGACY_SUMMARY_PREFIX``. Spelled here,
# not imported, so a state read never pulls the compressor in; ``test_resume_compactions`` pins both to the
# compressor's constants.
CARRIER_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")

_SQL = ("SELECT COUNT(DISTINCT COALESCE(message_uid, 'id:' || id)) FROM messages "
        "WHERE session_id = ? AND role = 'user' AND (" + " OR ".join("content LIKE ?" for _ in CARRIER_PREFIXES) + ")")


def stored_compactions(db: Any, session_id: Optional[str]) -> Optional[int]:
    """Distinct compaction carriers stored for ``session_id`` (any generation, active or not, deduplicated by
    ``message_uid``). ``None`` when it cannot be read: a client must treat that as unknown, never as zero."""
    if db is None or not session_id:
        return None
    try:
        row = db._read_one(_SQL, (session_id, *(p + "%" for p in CARRIER_PREFIXES)))
    except Exception:
        logger.debug("compaction count unavailable for %s", session_id, exc_info=True)
        return None
    return int(row[0]) if row is not None and row[0] is not None else None
