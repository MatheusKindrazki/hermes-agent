from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from hermes_cli.notification_inbox import enabled as inbox_enabled
from tools.bot_delivery_queue import enabled as queue_enabled
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(params=[(inbox_enabled, "notifications", "isolated_inbox"),
                        (queue_enabled, "bot_mode", "durable_delivery_queue")])
def setting(request):
    return request.param


def write_flag(home, section, key, value):
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(f"{section}:\n  {key}: {value}\n")


def test_delivery_honors_managed_overlay(tmp_path, monkeypatch, setting):
    enabled, section, key = setting
    profile, managed = tmp_path / "profile", tmp_path / "managed"
    write_flag(profile, section, key, "false")
    write_flag(managed, section, key, "true")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    assert enabled(profile) is True
    write_flag(managed, section, key, "false")
    assert enabled(profile) is False


def test_explicit_profiles_are_isolated_and_reload(tmp_path, setting):
    enabled, section, key = setting
    a, b = tmp_path / "a", tmp_path / "b"
    write_flag(a, section, key, "true")
    write_flag(b, section, key, "false")
    previous = get_hermes_home()
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(10):
            assert list(pool.map(enabled, [a, b])) == [True, False]
    assert get_hermes_home() == previous
    write_flag(a, section, key, "false")
    assert enabled(a) is False


def test_invalid_profile_does_not_silently_change_delivery(tmp_path, setting):
    enabled, section, key = setting
    write_flag(tmp_path, section, key, "true")
    assert enabled(tmp_path) is True
    (tmp_path / "config.yaml").write_text("broken: [")
    previous = get_hermes_home()
    with pytest.raises(yaml.YAMLError):
        enabled(tmp_path)
    assert get_hermes_home() == previous


def test_missing_profile_is_disabled(tmp_path, setting):
    enabled, _, _ = setting
    assert enabled(tmp_path / "absent") is False
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("bad", ["broken: [", "[]", "false"])
def test_invalid_managed_policy_never_changes_route(tmp_path, monkeypatch, setting, bad):
    enabled, section, key = setting
    profile, managed = tmp_path / "profile", tmp_path / "managed"
    write_flag(profile, section, key, "false")
    write_flag(managed, section, key, "true")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    assert enabled(profile) is True
    (managed / "config.yaml").write_text(bad)
    with pytest.raises((yaml.YAMLError, ValueError)):
        enabled(profile)


@pytest.mark.parametrize("bad", ["true", "bad-section", "[]"])
def test_invalid_section_does_not_select_legacy_route(tmp_path, setting, bad):
    enabled, section, _ = setting
    (tmp_path / "config.yaml").write_text(f"{section}: {bad}\n")
    with pytest.raises(ValueError):
        enabled(tmp_path)


@pytest.mark.parametrize("bad", ['"true"', "1", "[]", "{}", "null"])
def test_invalid_flag_does_not_select_legacy_route(tmp_path, setting, bad):
    enabled, section, key = setting
    write_flag(tmp_path, section, key, bad)
    with pytest.raises(ValueError):
        enabled(tmp_path)
