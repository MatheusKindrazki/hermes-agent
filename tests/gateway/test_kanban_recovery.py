"""Real dispatch preserves custom board identity and unknown attempts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


# The probe traces the transport client's opens with strace (Linux-only tool, Linux openat
# flags); without it the probe cannot prove the client never opens state.db writable.
@pytest.mark.platforms('linux')
@pytest.mark.skipif(shutil.which('strace') is None, reason='requires strace on PATH')
@pytest.mark.parametrize('mode', ['custom', 'cross_profile', 'restart'])
def test_owner_board_recovery(tmp_path, mode):
    repo = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    launcher = tmp_path / 'worker-path.sh'
    launcher.write_text('export HERMES_PYTEST_WORKER_PATH=1\n')
    launcher.chmod(0o700)
    env.update(HERMES_WORKER_PATH_LIB=str(launcher),
               K5_WORKER_PATH_SHA256=hashlib.sha256(launcher.read_bytes()).hexdigest())
    env.update(HOME=str(tmp_path / 'user'), HERMES_HOME=str(tmp_path / 'state'),
               PYTHONPATH=str(repo), KANBAN_RECOVERY_MODE=mode)
    result = subprocess.run([sys.executable, str(repo / 'tests/gateway/fixtures/kanban_recovery_probe.py')],
                            cwd=repo, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    print(json.dumps(json.loads((tmp_path / 'state/receipt.json').read_text())))
