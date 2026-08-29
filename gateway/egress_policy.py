"""Default-off identity and approval policy for external egress."""

from __future__ import annotations

import logging
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_VALID_MODES = frozenset({"off", "shadow", "enforce"})
_IDENTITY_FIELDS = (
    "profile",
    "tenant",
    "machine",
    "work_id",
    "session_id",
    "policy_version",
)
_HIGH_RISK_ACTIONS = frozenset({"delete", "publish", "transfer", "high_risk_send"})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class EgressSettings:
    mode: str
    profile: str = ""
    tenant: str = ""
    machine: str = ""
    policy_version: str = "tone-v1"


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    decision: str
    reasons: Tuple[str, ...]
    mode: str
    would_block: bool
    envelope: Mapping[str, Any]

    def metadata(self) -> dict[str, Any]:
        """Return the content-free audit projection allowed in shadow mode."""
        allowed = {
            "schema",
            "profile",
            "tenant",
            "machine",
            "work_id",
            "session_id",
            "policy_version",
            "action",
            "risk",
            "explicit_override",
            "idempotency_key",
            "payload_ref",
        }
        result = {key: self.envelope.get(key) for key in allowed if key in self.envelope}
        result.update(
            {
                "decision": self.decision,
                "reasons": list(self.reasons),
                "mode": self.mode,
                "would_block": self.would_block,
            }
        )
        return result


def resolve_egress_settings(
    config: Optional[Mapping[str, Any]] = None,
) -> EgressSettings:
    """Resolve ``gateway.reliability.egress.mode``; missing/invalid means off."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            logger.warning("Could not load egress policy config; using off")
            config = {}
    gateway = _mapping(_mapping(config).get("gateway"))
    reliability = _mapping(gateway.get("reliability"))
    raw = _mapping(reliability.get("egress"))
    candidate = str(raw.get("mode", "off")).strip().lower()
    mode = candidate if candidate in _VALID_MODES else "off"
    if candidate not in _VALID_MODES:
        logger.warning(
            "Ignoring gateway.reliability.egress.mode=%r; expected off, shadow, or enforce",
            candidate,
        )
    return EgressSettings(
        mode=mode,
        profile=str(raw.get("profile") or "").strip(),
        tenant=str(raw.get("tenant") or "").strip(),
        machine=str(raw.get("machine") or "").strip(),
        policy_version=str(raw.get("policy_version") or "tone-v1").strip()
        or "tone-v1",
    )


class EgressPolicy:
    def __init__(self, settings: EgressSettings):
        self.settings = settings

    @classmethod
    def from_config(
        cls, config: Optional[Mapping[str, Any]] = None
    ) -> "EgressPolicy":
        return cls(resolve_egress_settings(config))

    @property
    def mode(self) -> str:
        return self.settings.mode

    def build_envelope(
        self,
        *,
        action: str,
        destination: str,
        content: bytes,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Build v1 identity metadata without embedding message content."""
        supplied = _mapping(metadata)
        nested = _mapping(supplied.get("egress"))

        def value(name: str, default: Any = "") -> Any:
            if name in nested:
                return nested.get(name)
            if name in supplied:
                return supplied.get(name)
            return default

        try:
            from gateway.session_context import get_session_env

            session_profile = get_session_env("HERMES_SESSION_PROFILE", "")
            session_id = get_session_env("HERMES_SESSION_ID", "")
        except Exception:
            session_profile = ""
            session_id = ""

        digest = hashlib.sha256(content).hexdigest()
        envelope = {
            "schema": "kindra.egress/v1",
            "profile": str(
                value("profile", session_profile or self.settings.profile) or ""
            ),
            "tenant": str(value("tenant", self.settings.tenant) or ""),
            "machine": str(value("machine", self.settings.machine) or ""),
            "work_id": str(value("work_id", "") or ""),
            "session_id": str(value("session_id", session_id) or ""),
            "policy_version": str(
                value("policy_version", self.settings.policy_version) or ""
            ),
            "action": str(value("action", action) or action),
            "risk": str(value("risk", "normal") or "normal"),
            "explicit_override": value("explicit_override", False) is True,
            "idempotency_key": str(value("idempotency_key", digest) or digest),
            "payload_ref": str(
                value("payload_ref", f"owner-only://sha256/{digest}")
                or f"owner-only://sha256/{digest}"
            ),
        }
        for optional in ("approval_ref", "tone_repaired"):
            optional_value = value(optional, None)
            if optional_value is not None:
                envelope[optional] = optional_value
        return envelope

    def evaluate_delivery(
        self,
        *,
        action: str,
        destination: str,
        content: bytes,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> EgressDecision:
        envelope = self.build_envelope(
            action=action,
            destination=destination,
            content=content,
            metadata=metadata,
        )
        try:
            from gateway.session_context import get_session_env

            session_profile = get_session_env("HERMES_SESSION_PROFILE", "")
        except Exception:
            session_profile = ""
        supplied = _mapping(metadata)
        expected_profile = session_profile or self.settings.profile or None
        expected_tenant = (
            self.settings.tenant
            or str(supplied.get("expected_tenant") or "").strip()
            or None
        )
        return self.evaluate(
            envelope,
            expected_profile=expected_profile,
            expected_tenant=expected_tenant,
        )

    def prepare_metadata(
        self,
        *,
        action: str,
        destination: str,
        content: bytes,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> tuple[dict[str, Any], EgressDecision]:
        original = dict(metadata or {})
        decision = self.evaluate_delivery(
            action=action,
            destination=destination,
            content=content,
            metadata=original,
        )
        if self.mode != "off":
            original["egress_policy"] = decision.metadata()
        return original, decision

    def evaluate(
        self,
        envelope: Mapping[str, Any],
        *,
        expected_profile: Optional[str] = None,
        expected_tenant: Optional[str] = None,
    ) -> EgressDecision:
        """Evaluate metadata only; message content never enters this policy."""
        if self.mode == "off":
            return EgressDecision(True, "approved", (), "off", False, envelope)

        reasons: list[str] = []
        if envelope.get("schema") != "kindra.egress/v1":
            reasons.append("invalid_schema")
        for field in _IDENTITY_FIELDS:
            value = envelope.get(field)
            if not isinstance(value, str) or not value.strip():
                reasons.append(f"missing_identity:{field}")

        profile = envelope.get("profile")
        tenant = envelope.get("tenant")
        if expected_profile is not None and profile != expected_profile:
            reasons.append("profile_mismatch")
        if expected_tenant is not None and tenant != expected_tenant:
            reasons.append("tenant_mismatch")

        action = envelope.get("action")
        risk = envelope.get("risk")
        approval_ref = envelope.get("approval_ref")
        has_approval = isinstance(approval_ref, str) and bool(approval_ref.strip())
        is_high_risk = risk == "high" or action in _HIGH_RISK_ACTIONS
        if is_high_risk and not has_approval:
            reasons.append("approval_required")
        if envelope.get("explicit_override") is True and not has_approval:
            reasons.append("explicit_override_missing_approval")

        would_block = bool(reasons)
        allowed = self.mode == "shadow" or not would_block
        if would_block:
            decision = "rejected"
        elif envelope.get("explicit_override") is True:
            decision = "explicit_override"
        elif envelope.get("tone_repaired") is True:
            decision = "repaired"
        else:
            decision = "approved"
        return EgressDecision(
            allowed=allowed,
            decision=decision,
            reasons=tuple(reasons),
            mode=self.mode,
            would_block=would_block,
            envelope=envelope,
        )
