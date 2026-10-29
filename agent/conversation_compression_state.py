"""Snapshot restoration and persisted compression-state reads."""
from __future__ import annotations
import copy
from typing import Any, Optional

def _strip_marker_for_comparison(msgs: Any) -> Any:
    """Copy ``msgs`` with the ``_db_persisted`` marker removed for no-op comparison.
    Live dicts carry the marker while ``compress()`` output is swept, so a raw ``==`` would misclassify an
    identical no-op copy as progress. Non-list inputs and non-dict entries pass through unchanged."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    if not isinstance(msgs, list):
        return msgs
    return [{k: v for k, v in m.items() if k != _DB_PERSISTED_MARKER} if isinstance(m, dict) else m for m in msgs]


def _session_was_rotated_by_compression(session_db: Any, session_id: str) -> bool:
    """Return whether another path already rotated this compression parent."""
    getter = getattr(type(session_db), "get_session", None)
    if not callable(getter):
        return False
    session = getter(session_db, session_id)
    return bool(session and session.get("ended_at") is not None and session.get("end_reason") == "compression")


def _existing_system_prompt(agent: Any, system_message: str) -> str:
    """Cached system prompt, or a fresh build when nothing is cached (abort paths)."""
    return getattr(agent, "_cached_system_prompt", None) or agent._build_system_prompt(system_message)


def _restore_messages_snapshot(messages: list, snapshot: Optional[list]) -> None:
    """Put the pre-compression deep snapshot back into the live list if it drifted."""
    if snapshot is not None and messages != snapshot:
        messages[:] = copy.deepcopy(snapshot)


def _restore_prune_rearm_tokens(compressor: Any, snapshot: dict) -> None:
    """Restore ONLY the prune runway from the attempt snapshot.
    compress() zeroes it in memory while the durable copy only clears on a successful commit; a kept
    transcript keeps its cached prefix, and 0 would let the next prune break that cache."""
    if "_proactive_prune_rearm_tokens" in snapshot:
        compressor._proactive_prune_rearm_tokens = snapshot["_proactive_prune_rearm_tokens"]
