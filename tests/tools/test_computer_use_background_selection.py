"""A background target selection must not wait for an invisible approval."""
import json

import pytest

from tools.computer_use import tool as cu


@pytest.mark.parametrize("args", [{"action": "focus_app", "app": "Aside"},
                                  {"action": "focus_app", "app": "Aside", "raise_window": False}])
def test_background_selection_does_not_prompt(monkeypatch, args):
    monkeypatch.setenv("HERMES_COMPUTER_USE_BACKEND", "noop")
    cu.reset_backend_for_tests()
    prompts = []
    cu.set_approval_callback(lambda action, *_: prompts.append(action) or "timeout")
    try:
        result = json.loads(cu.handle_computer_use(args))
        assert not prompts, result
        assert result["ok"] is True
    finally:
        cu.set_approval_callback(None)
        cu.reset_backend_for_tests()


@pytest.mark.parametrize("args", [
    {"action": "focus_app", "app": "Aside", "raise_window": True},
    {"action": "click", "element": 1},
    {"action": "type", "text": "test"},
])
def test_ui_mutations_still_require_approval(monkeypatch, args):
    monkeypatch.setenv("HERMES_COMPUTER_USE_BACKEND", "noop")
    cu.reset_backend_for_tests()
    prompts = []
    cu.set_approval_callback(lambda action, *_: prompts.append(action) or "deny")
    try:
        result = json.loads(cu.handle_computer_use(args))
        assert prompts
        assert result["error"] == "denied by user"
    finally:
        cu.set_approval_callback(None)
        cu.reset_backend_for_tests()
