"""A persisted delivery receipt prevents a duplicate CLI turn."""
from types import SimpleNamespace

import cli


def test_durable_delivery_replay_exits_success_without_running_agent(monkeypatch, capsys):
    import json
    import pytest
    from unittest.mock import Mock
    ack = {"target_session_id": "s-1", "schema": "hermes.delivery-ack.v1"}
    monkeypatch.setattr(cli, "_delivery_ack", lambda *args, **kwargs: ack)
    agent = SimpleNamespace(_session_db=object(), run_conversation=Mock())
    the_cli = SimpleNamespace(agent=agent, session_id="s-1")
    with pytest.raises(SystemExit) as stopped:
        cli._run_quiet_single_query(the_cli, "hello", delivery_envelope={"delivery_id": "same"})
    assert stopped.value.code == 0
    assert json.loads(capsys.readouterr().out)["delivery_ack"] == ack
    agent.run_conversation.assert_not_called()
