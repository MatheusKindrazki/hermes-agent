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


# ---- review of #33: the selection survives a live refresh (/reload-mcp, a plugin going live) and the
# read-back of an unbuilt session; an operator pin still wins.

import types as _types


def _refresh_setup(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: _CFG)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: ["file", "terminal", "skills"])
    monkeypatch.setattr(server, "_load_disabled_toolsets", lambda: None)
    monkeypatch.setattr(server, "_gui_surface_toolsets", lambda platform: set())
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_info", lambda agent, sess=None: {})
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    captured: dict = {}

    def fake_refresh(agent, enabled_override=None, **kwargs):
        captured[agent.platform] = enabled_override

    import tools.mcp_tool_agent as mcp_agent
    monkeypatch.setattr(mcp_agent, "refresh_agent_mcp_tools", fake_refresh)
    return captured


def test_live_voice_agent_keeps_the_voice_list_after_refresh(monkeypatch):
    captured = _refresh_setup(monkeypatch)
    voice = _types.SimpleNamespace(platform="iphone-voice", enabled_toolsets=sorted(_VOICE))
    desk = _types.SimpleNamespace(platform="desktop", enabled_toolsets=["file", "terminal", "skills"])
    monkeypatch.setattr(server, "_sessions", {"v": {"agent": voice}, "d": {"agent": desk}})
    server._refresh_live_sessions(None)
    assert captured["iphone-voice"] == sorted(_VOICE)  # not the CLI list
    assert "terminal" not in captured["iphone-voice"] and "skills" not in captured["iphone-voice"]
    assert captured["desktop"] == ["file", "terminal", "skills"]  # other surfaces unchanged


def test_unbuilt_voice_session_reads_back_the_voice_list(monkeypatch):
    _refresh_setup(monkeypatch)
    enabled, _disabled = server._session_toolsets({"source": "iphone-voice"})
    assert enabled == sorted(_VOICE)
    enabled, _disabled = server._session_toolsets({"source": "desktop"})
    assert enabled == ["file", "terminal", "skills"]


def test_operator_pin_wins_over_the_voice_list(monkeypatch):
    _refresh_setup(monkeypatch)
    monkeypatch.setenv("HERMES_TUI_TOOLSETS", "file")
    assert server._agent_toolsets("iphone-voice") == ["file", "terminal", "skills"]  # i.e. _load_enabled_toolsets
