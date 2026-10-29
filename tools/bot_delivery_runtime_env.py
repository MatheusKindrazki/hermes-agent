"""Environment for an internal delivery transport on its selected release."""
import os
from pathlib import Path

def _delivery_runtime_env() -> dict[str, str]:
    """Keep internal Hermes CLI children on the runner's selected release.

    Terminal tools intentionally remove Hermes-owned PYTHONPATH entries. The
    delivery runner restores its own source only for its internal transport;
    otherwise a borrowed interpreter can import an older editable installation.
    """
    from tools.environments.local import served_profile_child_env

    env = served_profile_child_env(inherit_credentials=True)
    root = str(Path(__file__).resolve().parents[1])
    entries = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep)
               if entry and entry != root]
    env["PYTHONPATH"] = os.pathsep.join([root, *entries])
    return env
