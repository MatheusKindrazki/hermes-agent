"""GatewayInboundAdmissionMixin owns the fork delivery boundary."""
from __future__ import annotations
import logging
from typing import Optional
from gateway.platforms.event import MessageEvent
logger = logging.getLogger("gateway.run")


class GatewayInboundAdmissionMixin:
    def _record_admitted_inbound_origin(self, event, source, is_internal):
        """Publish ingress provenance only after its durable admission succeeded."""
        if not is_internal:
            from agent.durable_admission import capture_effect_origin, platform_event_id, publish_effect_origin

            _raw_event = getattr(event, "message_id", None)
            if _raw_event in (None, ""):
                _raw_event = getattr(event, "platform_update_id", None)
            _platform = getattr(getattr(source, "platform", None), "value", "") or "hermes"
            publish_effect_origin(capture_effect_origin(
                self._session_key_for_source(source), platform_event_id(_platform, _raw_event),
            ))
        if not is_internal:
            from hermes_cli.observability.shared_metrics_events import record_gateway_slash_command
            record_gateway_slash_command(event)


    def _admit_durable_inbound(self, event, source, is_internal):
        """Return a refusal before any input rewrites or model execution."""
        try:
            from agent.durable_admission import ModeError as _K8ModeError
            from agent.durable_admission import resolve_mode as _k8_resolve_mode

            _k8_on = _k8_resolve_mode()
        except _K8ModeError as _k8_mode_exc:
            # A misconfigured mode is not "off". An operator who typo'd the
            # arming value asked for enforcement and must not silently get
            # none, so the turn stops here — before any config, subprocess or
            # model — with a stable refusal.
            logger.error("durable admission mode refused: %s", _k8_mode_exc)
            return (
                "This message was not processed: the durable-admission mode "
                "setting is not recognised, so the work ledger could not be "
                "consulted. Nothing has run."
            )
        except Exception:
            logger.debug("durable admission probe failed", exc_info=True)
            _k8_on = False
        if _k8_on and not is_internal:
            _k8_blocked = self._k8_admit_inbound(event, source)
            if _k8_blocked is not None:
                return _k8_blocked

        return None


    def _k8_admit_inbound(self, event: MessageEvent, source) -> Optional[str]:
        """Record the K8 Signal for one inbound message, before anything reads it.

        Returns None to continue, or the user-facing text to reply with when
        admission failed — in which case the gateway must not run the turn.

        Identity comes from the platform, never from the text. ``message_id``
        (or Telegram's ``platform_update_id``) is what makes a redelivery of the
        same message one Signal and two same-text messages two Signals. Armed
        with no stable identity, this fails closed: minting an id would turn
        every replay into a fresh input, which is the duplicate-Work bug.

        The observation timestamp is left at ``OBSERVED_AT_UNKNOWN``. Only an
        immutable upstream time may go there, and ``MessageEvent.timestamp``
        defaults to ``datetime.now()`` at construction — so on a redelivery it
        is a NEW value, which would fork the Signal. Passing the sentinel is the
        truthful answer until a per-adapter platform time is plumbed through.
        """
        from agent.durable_admission import (
            admit_input,
            blocked_turn_result,
            platform_event_id,
            publish_pre_admission,
        )

        platform = getattr(getattr(source, "platform", None), "value", "") or "hermes"
        raw_identity = getattr(event, "message_id", None)
        if raw_identity in (None, ""):
            raw_identity = getattr(event, "platform_update_id", None)
        event_id = platform_event_id(platform, raw_identity)
        session_id = self._session_key_for_source(source)

        if not event_id:
            logger.error(
                "K8 armed but %s gave no stable message identity; refusing the "
                "turn rather than minting one",
                platform,
            )
            return (
                "This message could not be recorded in the work ledger because "
                "the platform did not supply a stable message identity. Nothing "
                "has run."
            )

        outcome = admit_input(
            session_id=session_id,
            request_text=getattr(event, "text", "") or "",
            event_id=event_id,
        )
        publish_pre_admission(outcome)
        if outcome.mode_off or outcome.model_may_run:
            return None
        logger.warning(
            "K8 refused inbound message (%s); the turn will not run",
            outcome.reason_code,
        )
        return blocked_turn_result(self, outcome, None)["final_response"]

