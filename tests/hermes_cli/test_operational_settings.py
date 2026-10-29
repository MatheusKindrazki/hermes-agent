"""Config wins over legacy deployment values without crossing profile scope."""
from hermes_cli.operational_settings import operational_setting, shadow_producer_enabled


def test_explicit_config_disables_legacy_producer(monkeypatch):
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "1")
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda: {"operations": {"shadow_producer_enabled": False}})
    assert shadow_producer_enabled() is False


def test_legacy_value_is_read_through_scoped_config(monkeypatch):
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", dict)
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", "/launch-profile")
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda key: "/served-profile")
    assert operational_setting("shadow_receipt_dir") == "/served-profile"


def test_config_empty_value_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", "/legacy")
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda: {"operations": {"shadow_receipt_dir": ""}})
    assert operational_setting("shadow_receipt_dir") == ""


def test_real_profile_config_is_authoritative(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.config import _KNOWN_ROOT_KEYS

    assert "operations" in _KNOWN_ROOT_KEYS
    assert "shadow_producer_enabled" in DEFAULT_CONFIG["operations"]
    launch = tmp_path / "launch"
    served = tmp_path / "served"
    launch.mkdir()
    served.mkdir()
    (launch / "config.yaml").write_text("operations:\n  shadow_producer_enabled: true\n  shadow_receipt_dir: /launch\n")
    (served / "config.yaml").write_text("operations:\n  shadow_producer_enabled: false\n  shadow_receipt_dir: /served\n")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "1")
    token = set_hermes_home_override(str(served))
    try:
        assert shadow_producer_enabled() is False
        assert operational_setting("shadow_receipt_dir") == "/served"
    finally:
        reset_hermes_home_override(token)
    assert shadow_producer_enabled() is True
    assert operational_setting("shadow_receipt_dir") == "/launch"
