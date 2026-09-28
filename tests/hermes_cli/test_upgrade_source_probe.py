"""The upgrade source probe checks optional source publication without enabling extras."""
import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def source_probe(tmp_path):
    source = Path(__file__).parents[1] / "e2e/core/upgrade/test_upgrade_path.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    probe = next(ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "IMPORT_PROBE" for t in node.targets))
    root = tmp_path / "source"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\ninclude = ["optional_adapter"]\n', encoding="utf-8")
    for name in ("optional_adapter/__init__.py", "hermes_cli/__init__.py", "hermes_cli/main.py",
                 "run_agent.py", "hermes_state.py"):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("", encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "Probe Fixture")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    added = root / "optional_adapter/new_feature.py"
    added.write_text("import deliberately_uninstalled_optional_dependency\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "new optional source")
    def run():
        env = {**os.environ, "PYTHONPATH": str(root)}
        return subprocess.run([sys.executable, "-S", "-c", probe, str(root), base],
                              cwd=root, env=env, capture_output=True, text=True)
    return root, added, run


def test_added_optional_source_does_not_require_activating_extra(source_probe):
    _, _, run = source_probe
    result = run()
    assert result.returncode == 0, result.stderr


def test_added_source_missing_still_fails(source_probe):
    _, added, run = source_probe
    added.unlink()
    result = run()
    assert result.returncode != 0
    assert "optional_adapter.new_feature" in result.stderr
    assert "no source spec" in result.stderr


def test_core_entrypoint_import_failure_still_fails(source_probe):
    root, _, run = source_probe
    (root / "run_agent.py").write_text("import missing_required_dependency\n", encoding="utf-8")
    result = run()
    assert result.returncode != 0
    assert "run_agent: ModuleNotFoundError" in result.stderr
