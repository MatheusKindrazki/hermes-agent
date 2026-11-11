"""Regression tests for #35809 — compression-exhaustion auto-reset loop.

After compression is exhausted the gateway auto-resets the session so the
next message starts on a fresh, empty conversation (#9893 / #10063). That
guarantee regressed once the Telegram topic-binding heal landed
(#20470 / #29712 / #33414):

    1. Compression rotates ``session_entry.session_id`` to an oversized
       compressed *child* session mid-turn and the agent-result sync rewrites
       the ``(chat_id, thread_id) -> child`` topic binding.
    2. ``reset_session`` swaps in a clean, parentless session — but its return
       value was discarded and the topic binding was left pointing at the
       bloated child.
    3. On the next inbound message in that topic, the binding-heal walk
       ``switch_session``'d the freshly-reset lane *back* onto the bloated
       child, ``load_transcript`` reloaded the oversized transcript, and
       compression exhaustion re-fired — a new session id every loop.

The fix captures the fresh entry from ``reset_session`` and re-syncs the
topic binding to it (a no-op on non-topic lanes).

Behavioral checks cover the reset, topic binding, and durable turn handoff.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway import run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB


def test_exhaustion_handoff_rebinds_fresh_session():
    """A reset must return and bind the same fresh entry, preserving this turn."""
    from unittest.mock import AsyncMock, Mock
    from gateway.run import GatewayRunner

    async def scenario():
        runner = object.__new__(GatewayRunner)
        old = SimpleNamespace(session_id="oversized")
        fresh = SimpleNamespace(session_id="fresh")
        runner.session_store = None
        runner._async_session_store = SimpleNamespace(_store=None, reset_session=AsyncMock(return_value=fresh))
        runner._evict_cached_agent = Mock()
        runner._clear_conversation_scope = Mock()
        runner._persist_compression_handoff = AsyncMock(return_value=True)
        runner._sync_telegram_topic_binding = Mock()
        event = SimpleNamespace(text="keep this request", message_id="event-1")
        source = _make_source()
        _, result = await runner._hmwa_compression_exhaustion_reset(
            {"compression_exhausted": True}, "", old, "route", source,
            event=event, handoff_content=event.text,
        )
        assert result is fresh
        runner._persist_compression_handoff.assert_awaited_once_with(
            fresh, event=event, content=event.text, source_session_id="oversized")
        runner._sync_telegram_topic_binding.assert_called_once_with(
            source, fresh, reason="compression-exhausted-reset")
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Behavioral contract: reset yields a clean next-turn transcript
# ---------------------------------------------------------------------------
def _make_store(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    # Isolate the SQLite transcript store so we exercise per-session_id
    # transcripts without touching the developer's real state.db.
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


def _make_source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")


def _bloat(n):
    # Stand-in for the oversized, post-compression "child" transcript that
    # could not be compressed any further (#35809). Alternates roles so the
    # fixture is a valid conversation: load_transcript is a live-replay
    # restore site and heals alternation violations on load (#64934), so a
    # degenerate all-user transcript would be merged into one message.
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": "x" * 2000,
        }
        for i in range(n)
    ]


class TestAutoResetLoadsCleanContext:
    """#35809: after the gateway auto-resets a session because compression
    was exhausted, the NEXT turn must load an EMPTY transcript for the new
    session_id — never the bloated compressed-child transcript."""

    def test_next_turn_transcript_is_empty_after_auto_reset(self, tmp_path):
        store = _make_store(tmp_path)
        source = _make_source()

        entry = store.get_or_create_session(source)
        session_key = entry.session_key
        bloated_sid = entry.session_id
        store._db.create_session(
            session_id=bloated_sid, source="telegram", user_id="u1"
        )
        store._db.replace_messages(bloated_sid, _bloat(120))
        assert len(store.load_transcript(bloated_sid)) == 120  # precondition

        new_entry = store.reset_session(session_key)
        assert new_entry is not None
        assert new_entry.session_id != bloated_sid

        resolved = store.get_or_create_session(source)
        assert resolved.session_id == new_entry.session_id
        loaded = store.load_transcript(resolved.session_id)

        assert loaded == [], (
            f"Auto-reset must yield an empty context, got {len(loaded)} "
            f"messages — the bloated compressed child leaked into the new session."
        )
        # The old transcript is still searchable, not destroyed.
        assert len(store.load_transcript(bloated_sid)) == 120


def test_compression_exhaustion_handoff_is_persisted_exactly_once():
    """An ineffective compressor must not discard the active user turn."""

    async def scenario():
        runner = object.__new__(gateway_run.GatewayRunner)
        rows = []
        seen = set()

        async def has_message(_session_id, message_id):
            return message_id in seen

        async def append(_session_id, row, **_kwargs):
            rows.append(row)
            seen.add(row["message_id"])

        runner._async_session_store = SimpleNamespace(
            _store=None,
            has_platform_message_id=has_message,
            append_to_transcript=append,
        )
        runner.session_store = None
        entry = SimpleNamespace(session_id="fresh-handoff-session")
        content = [
            {"type": "text", "text": "preserve this checkpoint"},
            {"type": "image_url", "image_url": {"url": "file:///tmp/pending.png"}},
        ]
        event = SimpleNamespace(message_id="platform-turn-7", timestamp=123.0)

        first = await runner._persist_compression_handoff(
            entry, event=event, content=content, source_session_id="oversized-parent"
        )
        second = await runner._persist_compression_handoff(
            entry, event=event, content=content, source_session_id="oversized-parent"
        )

        assert first is True
        assert second is False
        assert rows == [
            {
                "role": "user",
                "content": content,
                "timestamp": 123.0,
                "message_id": "platform-turn-7",
                "_compression_handoff": {
                    "source_session_id": "oversized-parent",
                    "reason": "compression_exhausted",
                },
            }
        ]

    asyncio.run(scenario())
