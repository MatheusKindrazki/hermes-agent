"""Compaction/reload must preserve the session-scoped Bot Chat tool."""
from types import SimpleNamespace

import pytest

from tools.bot_mode_dm import ensure_message_agent_tool
from tools.mcp_tool import refresh_agent_mcp_tools


@pytest.mark.parametrize('content_aware', [False, True])
def test_refresh_preserves_bot_chat_messaging(tmp_path, monkeypatch, content_aware):
    (tmp_path/'profile.yaml').write_text('ui_meta:\n  hermes-bots: {}\n')
    tool = {'type':'function','function':{'name':'terminal','parameters':{}}}
    agent = SimpleNamespace(tools=[tool], valid_tool_names={'terminal'},
                            _session_title_hint='Bot Chat',
                            _session_db=SimpleNamespace(db_path=str(tmp_path/'state.db')))
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **_: [tool])
    assert ensure_message_agent_tool(agent)
    original = agent.tools
    for _ in range(3):
        assert refresh_agent_mcp_tools(agent,content_aware=content_aware) == set()
        assert agent.tools is original
        assert [t['function']['name'] for t in agent.tools].count('message_agent') == 1
        assert 'message_agent' in agent.valid_tool_names


@pytest.mark.parametrize('title,managed,enabled', [
    ('Other chat',True,True), ('Bot Chat',False,True), ('Bot Chat',True,False),
])
def test_refresh_does_not_grant_messaging_outside_bot_mode(tmp_path, monkeypatch, title, managed, enabled):
    if managed: (tmp_path/'profile.yaml').write_text('ui_meta:\n  hermes-bots: {}\n')
    agent=SimpleNamespace(tools=[],valid_tool_names=set(),_session_title_hint=title,
                          _bot_mode_protocol=enabled,
                          _session_db=SimpleNamespace(db_path=str(tmp_path/'state.db')))
    monkeypatch.setattr('model_tools.get_tool_definitions',lambda **_: [])
    refresh_agent_mcp_tools(agent,content_aware=True)
    assert 'message_agent' not in agent.valid_tool_names
    assert agent.tools == []
