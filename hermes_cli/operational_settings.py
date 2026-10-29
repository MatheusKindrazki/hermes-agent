"""Profile-scoped operational settings, with explicit legacy deployment migration.

Config is authoritative, including false/empty values. The legacy mapping keeps
existing fork deployments working until their settings are moved to config.yaml;
it is not a surface for adding new environment-based behavior.
"""
from __future__ import annotations

from typing import Any

_LEGACY_SETTINGS = {
    "shadow_receipt_dir": "HERMES_KERNEL_SHADOW_RECEIPT_DIR",
    "shadow_producer_enabled": "HERMES_KERNEL_SHADOW_PRODUCER_ENABLED",
    "tone_gate_bin": "HERMES_TONE_GATE_BIN",
    "tone_schema_sha256": "HERMES_TONE_SCHEMA_SHA256",
    "work_control_projection_enabled": "HERMES_WORK_CONTROL_PROJECTION_ENABLED",
    "work_control_projection_url": "HERMES_WORK_CONTROL_PROJECTION_URL",
}


def operational_setting(name: str, default: Any = "") -> Any:
    """Read the owning profile's config, then its deprecated deployment setting."""
    from hermes_cli.config import get_env_value
    from hermes_cli.config_effective import load_user_config_effective

    config = load_user_config_effective()
    settings = config.get("operations", {})
    if isinstance(settings, dict) and name in settings:
        return settings[name]
    legacy_name = _LEGACY_SETTINGS.get(name)
    if legacy_name is None:
        return default
    legacy = get_env_value(legacy_name)
    return default if legacy is None else legacy


def shadow_producer_enabled() -> bool:
    value = operational_setting("shadow_producer_enabled", False)
    return value is True or value == "1"
