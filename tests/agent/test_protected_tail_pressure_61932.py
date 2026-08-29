"""Algorithmic reproduction and regression for issue #61932.

After several in-place compactions a tool-heavy session can be short enough
that nearly every remaining message sits inside the protected recent tail,
yet those messages are huge completed ``read_file`` / tool outputs.  The
middle compress window is then empty or tiny, preflight makes no material
token progress, and the turn dies with::

    Context length exceeded (174,833 tokens). Cannot compress further.

This is the core compressor contract — not Desktop/Windows-specific.
"""

from __future__ import annotations

from unittest.mock import patch
from types import SimpleNamespace

import pytest

from agent.context_compressor import (
    ContextCompressor,
    _LEAN_DIGEST_CHUNK_CHARS,
    _LEAN_DIGEST_MAX_CHUNKS,
    _MAX_TAIL_MESSAGE_FLOOR,
    _PRESSURE_KEEP_RECENT_MESSAGES,
)
from agent.model_metadata import estimate_messages_tokens_rough
from agent.turn_context import _compression_made_progress


def _unique_tool_pair(i: int, chars: int) -> list[dict]:
    """Assistant tool_call + unique tool result (no dedupe shortcut)."""
    body = f"FILE_{i}_START\n" + (f"line {i} unique payload " * (chars // 22))[:chars]
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"path":"f{i}.py"}}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": body,
        },
    ]


def _already_compacted_session(
    *,
    n_pairs: int,
    tool_chars: int,
    user_chars: int,
) -> list[dict]:
    """Shape after multiple in-place compactions: head + handoff + heavy tail."""
    msgs: list[dict] = [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "Investigate thoroughly"},
        {"role": "assistant", "content": "OK"},
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
                + ("Prior findings. " * 200)
            ),
        },
        {"role": "assistant", "content": "Continuing from compacted context."},
    ]
    for i in range(n_pairs):
        msgs.extend(_unique_tool_pair(i, tool_chars))
    msgs.append(
        {
            "role": "user",
            "content": "Full structured report:\n" + ("U" * user_chars),
        }
    )
    return msgs


@pytest.fixture()
def compressor_128k():
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=128_000,
    ):
        c = ContextCompressor(
            model="openai-codex/gpt-test",
            threshold_percent=0.50,
            summary_target_ratio=0.20,
            protect_first_n=3,
            protect_last_n=20,
            quiet_mode=True,
            config_context_length=128_000,
        )
    c._generate_summary = lambda *a, **k: "compact summary of earlier investigation"
    return c


class TestProtectedTailPressure61932:



    def test_compress_escapes_cannot_compress_further_dead_end(
        self, compressor_128k
    ):
        """Full compress path must materially reduce an over-context tail.

        Reproduces the #61932 failure class: multipass compression previously
        dropped a couple of message rows while leaving ~170k tokens intact,
        then reported no further progress.
        """
        c = compressor_128k
        msgs = _already_compacted_session(
            n_pairs=4, tool_chars=200_000, user_chars=80_000
        )
        rough0 = estimate_messages_tokens_rough(msgs)
        assert rough0 > c.context_length

        cur = msgs
        tok = rough0
        last_progress = False
        for _pass in range(3):
            o_len, o_tok = len(cur), tok
            out = c.compress(list(cur), current_tokens=tok)
            n_tok = estimate_messages_tokens_rough(out)
            last_progress = _compression_made_progress(
                o_len, len(out), o_tok, n_tok
            )
            cur, tok = out, n_tok
            if n_tok < c.threshold_tokens and n_tok < c.context_length:
                break

        assert tok < c.context_length, (
            f"still over context after compression: {tok:,} >= {c.context_length:,}"
        )
        assert tok < rough0 * 0.5, (
            f"compression did not reclaim enough headroom: {rough0:,} → {tok:,}"
        )
        # Either we recovered under threshold, or the last pass still made
        # progress (never a pure no-op dead-end above the window).
        assert tok < c.threshold_tokens or last_progress

    def test_hierarchical_fallback_bounds_every_chunk_and_preserves_live_tail(
        self, compressor_128k
    ):
        """A transcript above the model window must never create a giant chunk.

        The old max-chunk branch divided the entire region by 28, so a 1M+
        token history produced chunks larger than the auxiliary model window.
        The live user turn and its attachment/checkpoint must remain in the
        protected tail exactly once while older context is reduced in bounded
        map chunks.
        """
        c = compressor_128k
        del c._generate_summary
        c._SUMMARY_INPUT_MAX_CHARS = 40_000
        attachment_turn = {
            "role": "user",
            "content": [
                {"type": "text", "text": "continue with this checkpoint"},
                {"type": "image_url", "image_url": {"url": "file:///tmp/live.png"}},
            ],
            "checkpoint": {"reply_to_message_id": "reply-42"},
        }
        messages = [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "old acknowledgement"},
        ]
        for i in range(_LEAN_DIGEST_MAX_CHUNKS * 3):
            messages.extend(
                [
                    {"role": "user", "content": f"turn-{i}: " + ("u" * 24_000)},
                    {"role": "assistant", "content": f"result-{i}: " + ("a" * 24_000)},
                ]
            )
        messages.append(attachment_turn)
        before = estimate_messages_tokens_rough(messages)
        assert before > c.context_length

        prompts: list[str] = []

        def bounded_llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            prompts.append(prompt)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="bounded checkpoint"))]
            )

        with patch("agent.context_compressor.call_llm", side_effect=bounded_llm), patch(
            "agent.auxiliary_client.call_llm", side_effect=bounded_llm
        ):
            out = c.compress(messages, current_tokens=before)

        assert len(prompts) <= _LEAN_DIGEST_MAX_CHUNKS + 1
        digest_prompts = [p for p in prompts if "TRANSCRIPT SEGMENT:" in p]
        assert digest_prompts
        assert all(
            len(p) <= _LEAN_DIGEST_CHUNK_CHARS + 2_500 for p in digest_prompts
        )
        assert c._last_compression_telemetry["chunking"] is True
        assert c._last_compression_telemetry["chunk_count"] == len(digest_prompts)
        assert estimate_messages_tokens_rough(out) < c.context_length
        assert sum(message is attachment_turn for message in out) == 0
        preserved = [m for m in out if m.get("checkpoint") == attachment_turn["checkpoint"]]
        assert preserved == [attachment_turn]

    def test_all_oversized_tail_dead_end_shape_now_compresses(
        self, compressor_128k
    ):
        """Exact #61932 dead-end: the protected tail ALONE holds everything.

        Head (3 messages) + an 8-message tail of exclusively oversized tool
        pairs.  The tail token budget + the ``_MAX_TAIL_MESSAGE_FLOOR`` (8)
        floor protect every non-head message, so ``compress_start >=
        compress_end`` — pre-fix ``compress()`` returned the transcript
        UNCHANGED, incremented ``_ineffective_compression_count``, and the
        retry loop died with "Cannot compress further".  Post-fix the Phase-1
        pressure pass demotes the oversized tool bodies even though the
        summary window is empty, so the same call materially shrinks the
        transcript below the context window.
        """
        c = compressor_128k
        msgs: list[dict] = [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Investigate thoroughly"},
            {"role": "assistant", "content": "OK"},
        ]
        for i in range(4):
            msgs.extend(_unique_tool_pair(i, 200_000))
        assert len(msgs) == 11  # 3 head + 8-message all-oversized tail

        before = estimate_messages_tokens_rough(msgs)
        assert before > c.context_length, "fixture must start over-context"

        out = c.compress(list(msgs), current_tokens=before)
        after = estimate_messages_tokens_rough(out)

        # The dead-end is broken: one pass reclaims the bulk of the tail.
        assert after < c.context_length, (
            f"still over context: {after:,} >= {c.context_length:,}"
        )
        assert after < before * 0.25, (
            f"expected the oversized tail to demote: {before:,} → {after:,}"
        )

        # tool_call/tool_result pairing must survive demotion — never orphan
        # a tool result or a tool call (provider 400s otherwise).  Whole
        # pairs may legitimately be summarized away together.
        call_ids = {
            tc["id"]
            for m in out
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
            if isinstance(tc, dict)
        }
        tool_result_ids = [
            m.get("tool_call_id") for m in out if m.get("role") == "tool"
        ]
        assert tool_result_ids, "expected surviving tool pairs in the tail"
        for rid in tool_result_ids:
            assert rid in call_ids, f"orphaned tool result {rid!r}"
        for cid in call_ids:
            assert cid in tool_result_ids, f"orphaned tool call {cid!r}"
