"""HistoryMediaMixin owns one bounded execution phase of BasePlatformAdapter."""
from __future__ import annotations
import logging
from typing import Any, Optional, TYPE_CHECKING


class HistoryMediaMixin:
    def _history_media_paths_for_session(self, session_key: str) -> Optional[set]:
        """Return media paths already delivered in prior turns of this session
        (MEDIA: tags / image_generate payloads), so an echoed old tag isn't re-sent."""
        from gateway.platforms.base import (TranscriptReadError, logger)
        store = getattr(self, "_session_store", None)
        if not store:
            return None
        try:
            # Transcripts are keyed by session_id; map via the routing index, else the raw key.
            peek = getattr(store, "peek_session_id", None)
            session_id = peek(session_key) if callable(peek) else None
            transcript = store.load_transcript(session_id or session_key)
        except TranscriptReadError:
            logger.warning(
                "Transcript read failed for session %s; media dedup runs "
                "with no history this turn (#100788)", session_key,
            )
            return None
        except Exception:
            logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
            return None
        if not transcript:
            return None
        # Exclude the CURRENT TURN (from the last user message on): rows persist as produced, so a
        # text_to_speech media_tag would otherwise dedup away its own attachment.
        history = list(transcript)
        last_user_idx = next(
            (i for i in range(len(history) - 1, -1, -1) if history[i].get("role") == "user"), None)
        if last_user_idx is not None:
            history = history[:last_user_idx]
        else:
            # No user row (unusual store shape): at least drop the trailing reply.
            last_reply = next((msg for msg in reversed(history) if msg.get("role") == "assistant"), None)
            if last_reply is not None:
                history.remove(last_reply)
        if not history:
            return None
        from gateway.run_media_history import _collect_history_media_paths
        return _collect_history_media_paths(history)
