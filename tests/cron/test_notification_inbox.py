from unittest.mock import patch

import pytest

from cron import scheduler
from hermes_state import SessionDB


@pytest.mark.parametrize('delivery', ['bot-chat', 'bot-chat-notify', 'origin', 'telegram:123'])
def test_automatic_delivery_never_enters_human_chat(tmp_path, monkeypatch, delivery):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('notifications:\n  isolated_inbox: true\n')
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('human', 'cli')
    db.set_session_title('human', 'Bot Chat')
    db.append_message('human', 'user', 'Continue meu raciocínio')
    before = db.get_messages_as_conversation('human')
    job = {'id': 'test', 'name': 'Teste', 'deliver': delivery, 'fire_claim': {'at': 'today'}}
    with patch.object(scheduler.subprocess, 'run', side_effect=AssertionError('must not start model')):
        assert scheduler._deliver_result(job, 'Resultado material') is None
        assert scheduler._deliver_result(job, 'Resultado material') is None
    assert db.get_messages_as_conversation('human') == before
    inbox = db.get_session_by_title('Atualizações automáticas')
    assert inbox and not inbox.get('hidden')
    messages = db.get_messages_as_conversation(inbox['id'])
    assert len(messages) == 1
    assert 'Resultado material' in messages[0]['content']
    db.close()


def test_local_jobs_remain_local_and_inbox_needs_no_connector(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('notifications:\n  isolated_inbox: true\n')
    assert scheduler._preflight_check_delivery({'deliver': 'telegram:123'}) is None
    assert scheduler._deliver_result({'id': 'local', 'deliver': 'local'}, 'saved by scheduler') is None
    assert not (tmp_path / 'state.db').exists()


def test_existing_human_title_is_not_adopted(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('notifications:\n  isolated_inbox: true\n')
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('human', 'cli')
    db.set_session_title('human', 'Atualizações automáticas')
    error = scheduler._deliver_result({'id': 'j', 'deliver': 'bot-chat'}, 'automatic')
    assert 'belongs to a human conversation' in error
    assert db.get_messages_as_conversation('human') == []
    db.close()


def test_busy_inbox_never_falls_back_to_main(tmp_path, monkeypatch):
    from hermes_cli import notification_inbox
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('notifications:\n  isolated_inbox: true\n')
    result = notification_inbox.append(tmp_path, 'first', delivery_id='one', source='test')
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid = result['session_id']
    assert db.try_acquire_session_turn_lease(sid, 'test-live-human')
    error = scheduler._deliver_result({'id': 'j', 'deliver': 'bot-chat'}, 'second')
    assert 'busy' in error
    assert len(db.get_messages_as_conversation(sid)) == 1
    db.release_session_turn_lease(sid, 'test-live-human')
    assert scheduler._deliver_result({'id': 'j', 'deliver': 'bot-chat'}, 'second') is None
    assert len(db.get_messages_as_conversation(sid)) == 2
    db.close()
