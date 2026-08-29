"""Backend-owned correlation receipt for the Desktop identity canary."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional


ACTIVE_CONTEXT_SCHEMA = "kindra.active-context/v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def active_context_config(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    gateway = _mapping(_mapping(config).get("gateway"))
    reliability = _mapping(gateway.get("reliability"))
    return _mapping(reliability.get("active_context"))


def active_context_v1_enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Return the config.yaml canary gate; absent remains default-off."""
    value = active_context_config(config).get("enabled", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def build_active_context_receipt(
    *,
    config: Optional[Mapping[str, Any]],
    profile: str,
    session: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Build v1 only from explicit backend/route authority.

    Tenant, machine, connection and generation are intentionally required
    config values.  The function returns no receipt when any is missing, so a
    rollout cannot turn a profile name or host environment into a guessed
    identity.  Session ids come from the backend row itself.
    """
    if not active_context_v1_enabled(config):
        return None

    settings = active_context_config(config)
    required = {
        "connection_id": settings.get("connection_id"),
        "profile": profile,
        "tenant": settings.get("tenant"),
        "machine": settings.get("machine"),
        "gateway_generation": settings.get("gateway_generation"),
        "stored_session_id": session.get("id"),
    }
    normalized = {
        key: str(value).strip() if value is not None else ""
        for key, value in required.items()
    }
    if any(not value for value in normalized.values()):
        return None

    runtime_session_id = session.get("runtime_session_id") or session.get("id")
    return {
        "schema": ACTIVE_CONTEXT_SCHEMA,
        "connection_id": normalized["connection_id"],
        "profile": normalized["profile"],
        "tenant": normalized["tenant"],
        "machine": normalized["machine"],
        "gateway_generation": normalized["gateway_generation"],
        "runtime_session_id": str(runtime_session_id).strip(),
        "stored_session_id": normalized["stored_session_id"],
        "xirp_session_id": session.get("xirp_session_id"),
        "work_id": session.get("work_id"),
    }


def load_profile_config(home: Path) -> Mapping[str, Any]:
    """Load config.yaml under a context-local profile home."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(home)
    try:
        return load_config_readonly()
    finally:
        reset_hermes_home_override(token)

