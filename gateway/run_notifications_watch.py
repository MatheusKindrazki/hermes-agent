"""GatewayWatchNotificationsMixin owns one bounded execution phase of GatewayNotificationsMixin."""
from __future__ import annotations
import logging
from typing import Any, Optional, TYPE_CHECKING


class GatewayWatchNotificationsMixin:
    def _build_process_event_source(self, evt: dict):
        """Resolve the canonical source for a synthetic background-process event.

        Prefer the persisted session-store origin; the active foreground event causes cross-topic bleed.
        """
        from gateway.run_notifications import (Platform, SessionSource, _BUILTIN_PLATFORM_VALUES, _raw_process_event_session_id, logger)
        from gateway.run import _parse_session_key
        session_key = str(evt.get("session_key") or "").strip()
        derived = {}
        if session_key:
            try:
                self.session_store._ensure_loaded()
                entry = self.session_store._entries.get(session_key)
                if entry and getattr(entry, "origin", None):
                    return self._restored_source(entry)
            except Exception as exc:
                logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
                logger.debug("Synthetic process-event session-store lookup failed for %s: %s", session_key, exc)
            cached_source = self._get_cached_session_source(session_key)
            if cached_source is not None:
                return cached_source
            derived = _parse_session_key(session_key) or {}
        profile = derived.get("profile")
        platform_name = str(evt.get("platform") or derived.get("platform") or "").strip().lower()
        chat_type = str(evt.get("chat_type") or derived.get("chat_type") or "").strip().lower()
        chat_id = str(evt.get("chat_id") or derived.get("chat_id") or "").strip()
        if not platform_name or not chat_type or not chat_id:
            # Raw API keys legitimately have no messaging source. Resolve persisted
            # origins first, then leave this recognized route to the API dispatcher.
            if _raw_process_event_session_id(evt):
                return None
            logger.warning(
                "Synthetic event source unresolvable: "
                "session_key=%r platform=%r chat_type=%r chat_id=%r evt_type=%s",
                session_key, platform_name, chat_type, chat_id, evt.get("type", "?"),
            )
            return None
        try:
            platform = Platform(platform_name)
            # Reject dynamic pseudo-members: plugin platforms must be registered.
            if platform.value not in _BUILTIN_PLATFORM_VALUES:
                try:
                    from gateway.platform_registry import platform_registry
                    if not platform_registry.is_registered(platform.value):
                        raise ValueError(platform_name)
                except Exception as exc:
                    raise ValueError(platform_name) from exc
        except Exception:
            logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
            logger.warning("Synthetic process event has invalid platform metadata: %r", platform_name)
            return None

        def _opt(field: str) -> Optional[str]:
            return str(evt.get(field) or "").strip() or None

        scope_id = _opt("scope_id")
        if scope_id is None and chat_type not in ("dm", "thread"):
            # Reconstructed scoped-chat source without scope_id: a relay connector's tenant guard may
            # decline the reply. Warn, don't fail (native adapters need no scope_id).
            logger.warning(
                "Synthetic event source for %s chat=%s (%s) reconstructed "
                "without scope_id; scoped relay egress may be declined by "
                "the connector's tenant guard (user_id fallback only).", platform_name, chat_id, chat_type,
            )
        return SessionSource(
            platform=platform, chat_id=chat_id, chat_type=chat_type, thread_id=_opt("thread_id"),
            user_id=_opt("user_id"), user_name=_opt("user_name"), scope_id=scope_id, profile=profile,
        )


    async def _inject_watch_notification(
        self, synth_text: str, evt: dict, *, raise_not_accepted: bool = False,
    ) -> Optional[bool]:
        """Inject an internal notification unless it restates a fresh conclusion.

        Wraps :meth:`_inject_watch_notification_now` with the work-scoped
        supersession described in :meth:`_internal_event_equivalence_key`.

        The key is **reserved** for the duration of the injection, not merely
        checked before it. A bare check-await-record would let two concurrent
        injections of one conclusion both pass the check while the first was
        still awaiting the adapter, which is precisely the fan-out this exists
        to collapse. A second caller instead waits on the first attempt and
        only stands down if that attempt actually reached the adapter; if it
        did not, the second caller retries rather than being dropped, so a
        failed delivery never swallows the event behind it.

        Returning ``None`` for a superseded event matches the existing "another
        caller already owns this delivery" contract, so callers treat it as
        handled rather than replaying it forever. Suppression only ever removes
        a synthetic turn — it never splices text into a running one, so strict
        user/assistant alternation is untouched.
        """
        from gateway.run_notifications import (asyncio, logger, time)
        from gateway.run import _INTERNAL_EQUIVALENCE_TTL_SECONDS
        key = self._internal_event_equivalence_key(evt)
        if key is None:
            return await self._inject_watch_notification_now(synth_text, evt, raise_not_accepted=raise_not_accepted)

        while True:
            owner: Optional[asyncio.Future] = None
            waiter: Optional[asyncio.Future] = None
            with self._completion_delivery_lock:
                self._prune_internal_equivalence(time.monotonic())
                if self._internal_event_superseded_locked(key, evt):
                    logger.debug(
                        "Superseding internal %s event for %s: an equivalent "
                        "conclusion already reached this route within %.0fs",
                        evt.get("type", "completion"),
                        evt.get("session_id") or "<unknown>",
                        _INTERNAL_EQUIVALENCE_TTL_SECONDS,
                    )
                    return True if raise_not_accepted else None
                inflight = self._equivalence_inflight()
                waiter = inflight.get(key)
                if waiter is None:
                    owner = asyncio.get_running_loop().create_future()
                    inflight[key] = owner

            if owner is None:
                # Another injection of this same conclusion is in flight.
                delivered_by_other = False
                try:
                    delivered_by_other = bool(await asyncio.shield(waiter))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception('Watch notification attempt failed')
                    delivered_by_other = False
                if delivered_by_other:
                    return True if raise_not_accepted else None
                # It did not reach the adapter. This event must not be lost
                # behind it — loop and take the attempt ourselves.
                continue

            result: Optional[bool] = None
            try:
                result = await self._inject_watch_notification_now(synth_text, evt, raise_not_accepted=raise_not_accepted)
                return result
            finally:
                with self._completion_delivery_lock:
                    if self._equivalence_inflight().get(key) is owner:
                        self._equivalence_inflight().pop(key, None)
                    if result is True:
                        self._record_internal_equivalence_locked(key, evt)
                if not owner.done():
                    owner.set_result(result is True)


    async def _inject_watch_notification_now(
        self, synth_text: str, evt: dict, *, raise_not_accepted: bool = False,
    ) -> Optional[bool]:
        """Inject a watch/completion notification as a synthetic message event.

        Routing comes from the queued event, never the active foreground message. Returns
        ``True`` on adapter acceptance, ``False`` on retryable adapter failure, ``None`` with no
        gateway route or a refused notice pinned to a permanently-gone session (``_watch_pin_gone``).
        Not transactional: a crash after acceptance can replay (at-least-once).
        """
        from gateway.run_notifications import (MessageEvent, MessageType, Platform, _mark_internal_notification, _raw_process_event_session_id, _watch_notification_metadata, asyncio, logger)
        from gateway.wake import WakeNotAccepted, adapter_supports_push, admit_internal_event
        source = await asyncio.to_thread(self._build_process_event_source, evt)
        if not source:
            # API-server sessions bind the RAW X-Hermes-Session-Id key, not a structured ``agent:...`` key.
            raw_sid = _raw_process_event_session_id(evt)
            if raw_sid:
                adapter = self.adapters.get(Platform.API_SERVER)
                if adapter is not None and not adapter_supports_push(adapter):
                    return await self._self_post_api_server(adapter, synth_text, raw_sid, evt)
                logger.debug(
                    "Deferring watch notification for raw session %s: no api_server adapter to self-post through",
                    raw_sid,
                )
                return False
            logger.warning(
                "Dropping watch notification with no routing metadata for process %s",
                evt.get("session_id", "unknown"),
            )
            return None
        platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        adapter = self._resolve_injection_adapter(platform_name, source)
        if not adapter and platform_name == Platform.API_SERVER.value and getattr(source, "profile", None):
            # A route-only served profile owns no adapter map; the shared listener wakes exactly the
            # session that profile's store owns (proven in ``_self_post_api_server``), fail-closed.
            adapter = self.adapters.get(Platform.API_SERVER)
        if not adapter:
            return False
        if not adapter_supports_push(adapter):
            # Non-push adapter (api_server): its chat_id IS the raw session id, so handle_message would
            # key the wake under a build_session_key() that never matches — self-post instead.
            raw_sid = str(evt.get("origin_session_id") or "").strip() or str(source.chat_id or "")
            return await self._self_post_api_server(adapter, synth_text, raw_sid, evt)
        try:
            synth_text = _mark_internal_notification(synth_text)
            metadata = _watch_notification_metadata(evt, source)
            # The queued event's ``message_id`` is the message that STARTED the process, and the
            # persisted origin carries the same stale id. A synthetic completion is not a reply to
            # it: by delivery time the user has often continued elsewhere, and an event anchored
            # there makes the finished job's reply quote that old message on every reply-anchoring
            # platform (#52694: a background completion visibly replying to a stale Discord DM
            # message). Routing is unaffected — topic lanes carry thread_id and the anchor-less
            # synthetic-send branches are covered (#87051); the original id rides metadata for
            # debugging only.
            trigger_message_id = str(evt.get("message_id") or "").strip() or None
            if trigger_message_id:
                metadata["original_trigger_message_id"] = trigger_message_id
            if getattr(source, "message_id", None):
                from gateway.session_identity import replace_source
                source = replace_source(source, message_id=None)
            synth_event = MessageEvent(
                text=synth_text, message_type=MessageType.TEXT, source=source, internal=True,
                metadata=metadata,
            )
            logger.info(
                "Watch pattern notification — injecting for %s chat=%s thread=%s",
                platform_name, source.chat_id, source.thread_id,
            )
            # Relay egress priming: post-restart routing caches are cold (they warm only on inbound), so
            # replies would egress without tenant discriminators and be declined by the connector.
            _prime = getattr(adapter, "prime_routing_cache", None)
            if callable(_prime):
                _prime(synth_event)
            from gateway.session_authorities import active_authority
            authority = active_authority(self)
            if authority is not None:
                from gateway.session_automation import admit_process_notice
                from hermes_state_runtime import RuntimeStoreError
                try:
                    await admit_process_notice(self, authority, adapter, synth_event, evt)
                except RuntimeStoreError as exc:
                    raise WakeNotAccepted(str(exc), reason=exc.reason) from exc
            else:
                await admit_internal_event(adapter, synth_event)
            return True
        except WakeNotAccepted as exc:
            # Permanent only on proof: a pin the authority's state shows gone (/new, #141562) or a
            # final code. An unowned or foreign-pinned refusal may still find its owner (d6635ffe03b).
            from gateway.wake import PERMANENT_REFUSAL_REASONS
            pin_gone = await self._watch_pin_gone(evt, str(exc), log=not raise_not_accepted)
            exc.permanent = pin_gone or exc.reason in PERMANENT_REFUSAL_REASONS
            if pin_gone and not raise_not_accepted:
                return None
            if not raise_not_accepted:
                evt["_refusal_permanent"] = exc.permanent  # the drain counts only permanent refusals
            logger.warning("Watch notification not admitted for %s chat=%s: %s",
                           platform_name, source.chat_id, exc)
            # Durable callers refund the claim; ordinary watch callers just requeue.
            if raise_not_accepted:
                raise
            return False
        except Exception as e:
            logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
            logger.error("Watch notification injection error: %s", e)
            return False
