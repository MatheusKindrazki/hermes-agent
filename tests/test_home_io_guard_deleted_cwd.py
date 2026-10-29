"""Deleted working directories must not defeat protected-home metadata checks."""
import pytest

from tests.home_io_guard import HomeIOGuard


@pytest.mark.platforms("posix")
def test_deleted_cwd_with_relative_path_keeps_absolute_metadata_checks(monkeypatch, tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    secret = protected / "config.yaml"
    secret.write_text("secret: test\n")
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    scratch = tmp_path / "deleted-cwd"
    scratch.mkdir()
    guard = HomeIOGuard(lambda: (protected,))
    monkeypatch.setenv("PATH", f"relative-bin:{tmp_path / 'bin'}")
    monkeypatch.chdir(scratch)
    scratch.rmdir()
    try:
        # The absolute path is valid even though relative PATH entries cannot
        # be resolved; do not let FileNotFoundError turn is_dir() into False.
        guard.check(ordinary, metadata=True)
        with pytest.raises(AssertionError, match="REAL hermes home"):
            guard.check(secret, metadata=True)
        with pytest.raises(AssertionError, match="REAL hermes home"):
            guard.check(secret)
    finally:
        # Restore before pytest's own assertion rendering/stat calls.
        monkeypatch.chdir(tmp_path)
