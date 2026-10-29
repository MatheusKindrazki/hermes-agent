"""Focused tool pair whitespace behavior contracts."""

import json

import re

import sqlite3

import pytest

import time

import httpx

import openai

from unittest.mock import patch, MagicMock

from agent.context_compressor import (
    ContextCompressor,
    HISTORICAL_TASK_HEADING,
    SUMMARY_PREFIX,
    COMPRESSED_SUMMARY_METADATA_KEY,
    _PRUNE_MIN_CHARS,
    _summarize_tool_result,
    _sum_clarify,
    _is_summary_access_or_quota_error,
)

from agent.compression_marker import _COMPRESSION_MARKER_PREFIX

from hermes_state import SessionDB

from agent.auxiliary_client import CODEX_STREAM_STALL_MARKER

import itertools



class TestSanitizeToolPairsWhitespace:
    """_sanitize_tool_pairs must strip whitespace from tool_call_id before
    comparing, matching the fix applied to agent_runtime_helpers.py in
    commit fa3ab2ffd.  Without stripping, a valid tool result whose
    tool_call_id has surrounding whitespace is misclassified as orphaned
    and silently replaced with a [Result unavailable] stub.
    """

    def _make(self):
        with patch("agent.context_compressor.get_model_context_length", return_value=100000):
            return ContextCompressor(model="test/model", quiet_mode=True,
                                     protect_first_n=2, protect_last_n=2)

    def _assistant(self, call_id):
        return {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}],
        }

    def test_leading_whitespace_on_result_id_preserved(self):
        c = self._make()
        msgs = [
            self._assistant("call_abc"),
            {"role": "tool", "tool_call_id": " call_abc", "content": "ok"},
        ]
        out = c._sanitize_tool_pairs(msgs)
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "ok", "valid result must not be treated as orphaned"


    def test_truly_orphaned_still_removed(self):
        """Whitespace-trimmed ID that still has no match must be removed.
        The assistant's call_real has no matching result, so a stub is
        inserted in its place — the original orphaned entry must be gone."""
        c = self._make()
        msgs = [
            self._assistant("call_real"),
            {"role": "tool", "tool_call_id": " call_orphan ", "content": "stale"},
        ]
        out = c._sanitize_tool_pairs(msgs)
        tool_call_ids = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
        assert "call_orphan" not in tool_call_ids, "genuinely orphaned result must be removed"
        assert " call_orphan " not in tool_call_ids, "original whitespace form must also be gone"


# Shared fixtures are registered through pytest, not imported as unused names.
pytest_plugins = ('tests.agent.test_context_compressor',)
