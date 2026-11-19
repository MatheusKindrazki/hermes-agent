"""``tools.tool_search.eager``: names that never defer, MCP tools included.

Every MCP tool is deferrable by rule, and ``defer`` can only add to that set, so a deployment had
no way to keep the few connector tools a voice turn depends on in front of the model.
"""

from __future__ import annotations

import pytest

from tools.registry import registry
from tools.tool_search import (
    BRIDGE_TOOL_NAMES, ToolSearchConfig, assemble_tool_defs, is_deferrable_tool_name)

_PINNED = "mcp__eager_probe__search_memory"
_OTHER = "mcp__eager_probe__send_message"


def _td(name: str) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": f"{name} probe", "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture
def mcp_tools():
    for name in (_PINNED, _OTHER):
        registry.register(name=name, handler=lambda args, **kw: "{}", schema=_td(name)["function"],
                          toolset="mcp-eager-probe")
    yield
    for name in (_PINNED, _OTHER):
        registry.deregister(name)


def _visible(config: ToolSearchConfig) -> set[str]:
    assembled = assemble_tool_defs([_td("terminal"), _td(_PINNED), _td(_OTHER)],
                                   context_length=200_000, config=config)
    assert assembled.activated
    return {td["function"]["name"] for td in assembled.tool_defs}


def test_without_eager_every_mcp_tool_defers(mcp_tools):
    names = _visible(ToolSearchConfig.from_raw({"enabled": "on"}))
    assert ToolSearchConfig.from_raw({"enabled": "on"}).eager_tools == frozenset()
    assert _PINNED not in names and _OTHER not in names
    assert BRIDGE_TOOL_NAMES <= names


def test_eager_mcp_tool_stays_visible_and_the_rest_still_defer(mcp_tools):
    config = ToolSearchConfig.from_raw({"enabled": "on", "eager": [_PINNED]})
    names = _visible(config)
    assert _PINNED in names
    assert _OTHER not in names
    assert not is_deferrable_tool_name(_PINNED, config.effective_defer_tools, config.eager_tools)


def test_eager_wins_over_an_explicit_defer_entry(mcp_tools):
    config = ToolSearchConfig.from_raw({"enabled": "on", "defer": [_PINNED], "eager": [_PINNED]})
    assert _PINNED in _visible(config)


def test_scalar_eager_is_ignored():
    assert ToolSearchConfig.from_raw({"eager": _PINNED}).eager_tools == frozenset()
