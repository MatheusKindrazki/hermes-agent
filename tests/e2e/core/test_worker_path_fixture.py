"""The E2E approval fixture crosses the real signed source/exec boundary."""
import os
import subprocess

import pytest

from hermes_cli import kanban_db
from tests.e2e.core._worker_path import signed_worker_path_env

pytestmark = pytest.mark.platforms("linux", "macos")


def test_approved_harness_path_is_used_by_child_and_tampering_is_refused(tmp_path):
    tools = tmp_path / "tools with spaces"
    tools.mkdir()
    probe = tools / "worker-fixture-probe"
    probe.write_text("#!/bin/sh\nprintf 'worker-spawned\\n'\n")
    probe.chmod(0o700)
    approved = {**os.environ, "PATH": str(tools)}
    approved.update(signed_worker_path_env(tmp_path, approved))
    # The caller has a different PATH: only sourcing the approved image allows
    # the real child to find our executable.
    child_env = {**approved, "PATH": "/usr/bin:/bin"}
    snapshot = kanban_db._snapshot_worker_path_lib(child_env)
    with snapshot.open_source_image() as image:
        result = subprocess.run(
            ["/bin/bash", "-c", kanban_db._WORKER_PATH_EXEC_SCRIPT, "worker-test",
             snapshot.source_path_for(image.fileno()), "worker-fixture-probe"],
            pass_fds=(image.fileno(),), env=child_env, text=True, capture_output=True, check=True,
        )
    assert result.stdout == "worker-spawned\n"
    (tmp_path / "e2e-worker-path.sh").write_text("export PATH=/unexpected\n")
    with pytest.raises(RuntimeError, match="worker_path_hash_mismatch"):
        kanban_db._snapshot_worker_path_lib(child_env)
