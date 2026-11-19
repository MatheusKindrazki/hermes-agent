"""``voice.turn_policy``: voice sources build a lighter agent (no cwd AGENTS.md, no background review).

The policy is off unless config names the session source, so every other surface builds exactly as before.
"""

from __future__ import annotations

import types

import pytest

from tui_gateway import server

_POLICY = {"voice": {"turn_policy": {
    "sources": ["iphone-voice", "iphone-notebook", "watch"],
    "skip_context_files": True, "skip_background_review": True}}}


def _build(monkeypatch, cfg: dict, platform_override: str | None = None, session_source: str | None = None):
    captured: dict = {}

    def fake_agent(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(model=kwargs.get("model"))

    for var in ("HERMES_MODEL", "HERMES_INFERENCE_MODEL", "HERMES_TUI_PROVIDER", "HERMES_DESKTOP",
                "HERMES_DESKTOP_TERMINAL", "HERMES_IGNORE_RULES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "_load_cfg",
                        lambda: {**cfg, "model": {"default": "gpt-5.5", "provider": "openai-codex"}})
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None, target_model=None: {
            "provider": "openai-codex", "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "token", "api_mode": "codex_responses", "credential_pool": None})
    monkeypatch.setattr("run_agent.AIAgent", fake_agent)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: ["file"])
    monkeypatch.setattr(server, "_get_db", lambda: None)
    if session_source is not None:
        monkeypatch.setitem(server._sessions, "sid", {"source": session_source})
    server._make_agent("sid", "session-key", platform_override=platform_override)
    return captured


def _flags(captured: dict) -> tuple:
    return (captured.get("skip_context_files"), captured.get("load_soul_identity"),
            captured.get("skip_background_review"))


@pytest.mark.parametrize("source", ["iphone-voice", "iphone-notebook", "watch"])
def test_voice_source_builds_the_lighter_agent(monkeypatch, source):
    assert _flags(_build(monkeypatch, _POLICY, platform_override=source)) == (True, True, True)


def test_rebuild_without_platform_override_reads_the_session_source(monkeypatch):
    assert _flags(_build(monkeypatch, _POLICY, session_source="iphone-voice")) == (True, True, True)


def test_non_voice_source_is_unchanged(monkeypatch):
    assert _flags(_build(monkeypatch, _POLICY, platform_override="desktop")) == (False, False, False)


def test_no_policy_config_is_unchanged_for_voice_sources(monkeypatch):
    assert _flags(_build(monkeypatch, {}, platform_override="iphone-voice")) == (False, False, False)


def test_each_switch_is_independent(monkeypatch):
    cfg = {"voice": {"turn_policy": {"sources": ["watch"], "skip_background_review": True}}}
    assert _flags(_build(monkeypatch, cfg, platform_override="watch")) == (False, False, True)
