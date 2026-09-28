"""Approved, local worker PATH image for real-process fork E2E harnesses."""
from __future__ import annotations

import hashlib
import shlex
from pathlib import Path
from typing import Mapping


def signed_worker_path_env(root: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Pin the harness PATH without relying on a developer's external K5 checkout.

    The production dispatcher still opens, hashes, probes and fd-pins this image.
    Its bytes export the actual harness tool path (including upgrade wrappers),
    so spawned workers exercise the same admission and source/exec boundary.
    This is test-owned approval, not a production library or a guard bypass.
    """
    library = root.resolve() / "e2e-worker-path.sh"
    content = f"export PATH={shlex.quote(env['PATH'])}\n"
    library.write_text(content, encoding="utf-8")
    library.chmod(0o600)
    return {
        "HERMES_WORKER_PATH_LIB": str(library),
        "K5_WORKER_PATH_SHA256": hashlib.sha256(library.read_bytes()).hexdigest(),
    }
