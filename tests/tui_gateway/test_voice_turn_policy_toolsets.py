"""``voice.turn_policy.toolsets`` / ``max_iterations``: a voice turn carries only the voice toolsets and a
short tool-round budget. Measured 2026-11-21 on the Mini: a voice session re-sent 44 visible tool schemas
(terminal, files, browser, kanban, skills…) and the Chief prompt on every API call (~35-39k tokens before
any history), and a recall question took 8 serial API calls (52 s), two of them loading skills."""

from __future__ import annotations

import pytest

from tests.tui_gateway.test_voice_turn_policy import _build
from tui_gateway import server
from tui_gateway.voice_turn_policy import resolve_voice_turn_policy

_VOICE = ["web", "vision", "memory", "session_search", "clarify", "mcp-front-memory", "mcp-voice-notebook"]
_CFG = {"voice": {"turn_policy": {"sources": ["iphone-voice", "watch"], "toolsets": _VOICE, "max_iterations": 6}}}


def test_voice_source_gets_only_the_voice_toolsets(monkeypatch):
    monkeypatch.setattr(server, "_load_disabled_toolsets", lambda: None)
    monkeypatch.setattr(server, "_gui_surface_toolsets", lambda platform: set())
    captured = _build(monkeypatch, _CFG, platform_override="iphone-voice")
    assert captured["enabled_toolsets"] == sorted(_VOICE)
    assert "file" not in captured["enabled_toolsets"]  # the CLI list (stubbed as ["file"]) is replaced
    assert captured["max_iterations"] == 6


def test_disabled_toolsets_and_surface_fold_in_still_apply(monkeypatch):
    monkeypatch.setattr(server, "_load_disabled_toolsets", lambda: ["web"])
    monkeypatch.setattr(server, "_gui_surface_toolsets", lambda platform: {"desktop_ui"})
    captured = _build(monkeypatch, _CFG, platform_override="watch")
    assert "web" not in captured["enabled_toolsets"]
    assert "desktop_ui" in captured["enabled_toolsets"]


def test_other_sources_keep_the_cli_toolsets_and_budget(monkeypatch):
    captured = _build(monkeypatch, _CFG, platform_override="desktop")
    assert captured["enabled_toolsets"] == ["file"]
    assert captured["max_iterations"] == 500


def test_no_toolsets_key_keeps_the_cli_list_for_voice(monkeypatch):
    cfg = {"voice": {"turn_policy": {"sources": ["iphone-voice"], "skip_background_review": True}}}
    captured = _build(monkeypatch, cfg, platform_override="iphone-voice")
    assert captured["enabled_toolsets"] == ["file"]
    assert captured["max_iterations"] == 500


@pytest.mark.parametrize("raw,expected", [
    (["web", " ", "web", "memory"], ("web", "memory")),  # blanks dropped, duplicates once, order kept
    ([], None),            # an empty list would leave the voice with no tools: ignored
    ("web", None),         # a scalar is not a list: ignored
    (None, None),
])
def test_toolsets_parsing(raw, expected):
    cfg = {"voice": {"turn_policy": {"sources": ["iphone-voice"], "toolsets": raw}}}
    assert resolve_voice_turn_policy(cfg, "iphone-voice").toolsets == expected


@pytest.mark.parametrize("raw,expected", [(6, 6), ("8", 8), (1, None), (0, None), (51, None), (True, None), ("x", None)])
def test_max_iterations_parsing(raw, expected):
    cfg = {"voice": {"turn_policy": {"sources": ["iphone-voice"], "max_iterations": raw}}}
    assert resolve_voice_turn_policy(cfg, "iphone-voice").max_iterations == expected
