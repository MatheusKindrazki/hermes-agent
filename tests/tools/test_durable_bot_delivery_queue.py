import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import bot_mode_dm as dm
from tools.bot_delivery_queue import Queue
from hermes_state import SessionDB

pytestmark = pytest.mark.macos_only


def test_local_acceptance_means_request_is_durably_queued(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    (home / 'profiles/staff').mkdir(parents=True)
    (home / 'config.yaml').write_text('bot_mode:\n  durable_delivery_queue: true\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(dm.tempfile, 'gettempdir', lambda: str(tmp_path))
    agent = SimpleNamespace(session_id='source', _session_db=SimpleNamespace(db_path=str(home / 'state.db')))
    captured = []
    monkeypatch.setattr(dm, '_spawn_delivery', lambda *a, **kw: captured.append(kw) or '{}')
    dm._start_delivery_inner(
        ['hermes', '-p', 'staff', 'chat', '--in', '~', '-c', 'Bot Chat', '--create-if-missing', '-Q'],
        'Pedido que não pode desaparecer', '@staff', stdin_file=False,
        task_id=None, agent=agent,
    )
    assert captured[0]['receipt']['state'] == 'queued'
    from tools.bot_delivery_queue import Queue
    rows = Queue(home).list()
    assert len(rows) == 1 and rows[0]['state'] == 'queued'
    assert Queue(home).request(rows[0]['id'])['content'] == 'Pedido que não pode desaparecer'
    assert captured[0]['dm_file'] is None


@pytest.fixture
def queue_fixture(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    root.mkdir()
    target = root / 'profiles/staff'
    target.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(dm.tempfile, 'gettempdir', lambda: str(tmp_path))
    db = SessionDB(db_path=target / 'state.db')
    db.create_session('recipient', 'cli')
    db.set_session_title('recipient', 'Bot Chat')
    # A real subprocess/SQLite transport, with no model or external effects.
    cli = tmp_path / 'hermes'
    cli.write_text(f'''#!{sys.executable}
import os,sys
from pathlib import Path
from hermes_state import SessionDB
h=Path(os.environ['HERMES_HOME'])
if h.parent.name=='profiles':h=h.parent.parent
profile=sys.argv[sys.argv.index('-p')+1]
body=Path(sys.argv[sys.argv.index('--query-file')+1]).read_text()
db=SessionDB(db_path=h/'profiles'/profile/'state.db')
db.append_message('recipient','user',body)
db.close()
print('reply: '+body)
''')
    cli.chmod(0o700)
    q = Queue(root)
    def enqueue(content='new request', identity=None):
        record = {'delivery_id': str(uuid.uuid4()), 'idempotency_key': uuid.uuid4().hex,
                  'origin_session_id': 'origin', 'state': 'accepted', 'accepted_at': int(time.time())}
        if identity:
            record['turn_identity'] = identity
        argv = [str(cli), '-p', 'staff', 'chat', '-c', 'Bot Chat', '-Q']
        return q.enqueue(argv, content, record, root)
    yield q, db, enqueue
    db.close()


def test_busy_target_retains_request_and_delivers_once_after_release(queue_fixture):
    q, db, enqueue = queue_fixture
    job = enqueue()
    assert db.try_acquire_session_turn_lease('recipient', 'human-busy')
    assert q.run_one(job['id']) == 'queued'
    assert db.get_messages_as_conversation('recipient') == []
    assert Queue(q.root).request(job['id'])['content'] == 'new request'
    db.release_session_turn_lease('recipient', 'human-busy')
    assert q.run_one(job['id']) == 'delivered'
    assert q.run_one(job['id']) == 'delivered'
    messages = db.get_messages_as_conversation('recipient')
    assert len(messages) == 1 and messages[0]['content'] == 'new request'
    assert (q.folder(job['id']) / 'reply.txt').read_text().strip() == 'reply: new request'


def test_fifo_and_enqueue_replay(queue_fixture):
    q, db, enqueue = queue_fixture
    a = enqueue('first')
    b = enqueue('second')
    request = q.request(a['id'])
    replay = q.enqueue(request['argv'], request['content'], {**request['record'], 'duplicate': True, 'state': 'queued'}, q.root)
    assert replay['id'] == a['id'] and len(q.list()) == 2
    assert q.run_one(b['id']) == 'queued'
    assert q.run_one(a['id']) == 'delivered'
    assert q.run_one(b['id']) == 'delivered'
    assert [m['content'] for m in db.get_messages_as_conversation('recipient')] == ['first', 'second']


def test_orphaned_dispatch_is_visible_unknown_and_never_replayed(queue_fixture):
    q, db, enqueue = queue_fixture
    job = enqueue()
    q.state(job['id'], 'running')  # crash after dispatch fence, before result
    restarted = Queue(q.root)
    assert restarted.run_one(job['id']) == 'unknown'
    assert restarted.run_one(job['id']) == 'unknown'
    assert restarted.request(job['id'])['content'] == 'new request'
    assert db.get_messages_as_conversation('recipient') == []


def test_expired_authority_never_calls_transport(queue_fixture, monkeypatch):
    q, db, enqueue = queue_fixture
    job = enqueue(identity={'request_key': 'expired-request'})
    def refuse(*args):
        raise RuntimeError('turn_identity_stale')
    monkeypatch.setattr(dm, '_revalidate_delivery_identity', refuse)
    assert q.run_one(job['id']) == 'failed'
    assert db.get_messages_as_conversation('recipient') == []
    assert q.request(job['id'])['content'] == 'new request'


def test_completion_listener_failure_does_not_lose_enqueued_request(tmp_path, monkeypatch):
    from tools import terminal_tool
    monkeypatch.setattr(terminal_tool, 'terminal_tool', lambda *a, **kw: json.dumps({'error': 'spawn failed'}))
    result = json.loads(dm._spawn_delivery('irrelevant', '@staff', receipt={'state': 'queued', 'queue_id': 'id'}, task_id=None, agent=None))
    assert result['status'] == 'queued'
    assert 'not received' in result['detail']


@pytest.mark.timeout(5)
def test_enforced_enqueue_under_existing_ledger_lock_does_not_deadlock(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    (home / 'profiles/staff').mkdir(parents=True)
    (home / 'config.yaml').write_text('bot_mode:\n  durable_delivery_queue: true\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(dm.tempfile, 'gettempdir', lambda: str(tmp_path))
    agent = SimpleNamespace(session_id='source', _session_db=SimpleNamespace(db_path=str(home / 'state.db')))
    key = 'a' * 64
    with dm._delivery_ledger_lock(key):
        prepared = dm._start_delivery_inner(
            ['hermes', '-p', 'staff', 'chat', '-c', 'Bot Chat', '-Q'],
            'enforced queued request', '@staff', stdin_file=False,
            task_id=None, agent=agent, _idempotency=key, _defer_spawn=True, _enforced=True,
        )
    assert isinstance(prepared, tuple)
    assert prepared[1] is None and prepared[2]['state'] == 'queued'


def test_worker_restart_retains_busy_job_and_delivers_after_release(queue_fixture):
    q, db, enqueue = queue_fixture
    job = enqueue('survives restart')
    assert db.try_acquire_session_turn_lease('recipient', 'human-busy')
    env = dict(os.environ, HERMES_HOME=str(q.root), PYTHONPATH=str(Path(dm.__file__).resolve().parents[1]))
    cmd = [sys.executable, '-m', 'tools.bot_delivery_queue', '--root', str(q.root), 'serve']
    def wait_for(predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            assert time.monotonic() < deadline
            time.sleep(.05)
    worker = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: (q.directory / 'health.json').exists())
        assert q.get(job['id'])['state'] == 'queued'
    finally:
        worker.terminate(); worker.wait(timeout=5)
    db.release_session_turn_lease('recipient', 'human-busy')
    worker = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: q.get(job['id'])['state'] == 'delivered')
        assert len(db.get_messages_as_conversation('recipient')) == 1
    finally:
        worker.terminate(); worker.wait(timeout=5)
