"""RelayDraftMixin owns one bounded execution phase of RelayAdapter."""
from __future__ import annotations
import logging
from typing import Any, Optional, TYPE_CHECKING


if TYPE_CHECKING:
    from gateway.platforms.base import SendResult


class RelayDraftMixin:
    async def send_draft(
        self, chat_id: str, draft_id: int, content: str, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        from gateway.relay.adapter import (SendResult, decline_error, is_egress_decline, log_decline, logger)
        if not self.supports_draft_streaming(chat_id=str(chat_id)):
            raise NotImplementedError("connector does not advertise the 'draft' relay op")
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        # Arm optimistically BEFORE the transport call (a lossy ack often means
        # delivered), but NEVER for a draft_id already sealed on this key: a straggler
        # after the seal re-armed interception with no live stream, and the next
        # unrelated send was wrongly converted into a seal.
        chat_key = self._draft_key(str(chat_id), metadata)
        if self._sealed_draft_by_chat.get(chat_key) == draft_id:
            # Post-seal straggler: content is already in the sealed message; report
            # success, send nothing. Log the FIRST swallow per key — one straggler is
            # the normal race, but a burst means something sealed a live stream
            # mid-flight (silence here cost a forensic hunt).
            if chat_key not in self._tombstone_swallow_logged:
                self._tombstone_swallow_logged[chat_key] = draft_id
                self._evict_oldest(self._tombstone_swallow_logged)
                logger.warning(
                    "relay: draft frame for %s swallowed by post-seal "
                    "tombstone (draft_id=%s) — expected for a straggler; "
                    "a live stream freezing NOW means something sealed it "
                    "mid-flight",
                    chat_key,
                    draft_id,
                )
            return SendResult(success=True)
        # Arm seal-interception ONLY for stream-is-the-message chats: on a
        # Telegram-shaped connector the final MUST go out as a real send.
        if self.stream_is_message_for_chat(str(chat_id)):
            self._open_draft_by_chat[chat_key] = draft_id
            self._evict_oldest(self._open_draft_by_chat)
        try:
            result = await self._outbound(chat_id, self._draft_frame(chat_id, draft_id, content, False, metadata))
        except Exception as e:
            # Ambiguous (stale socket, mid-write drop): may have been delivered;
            # keep interception armed.
            logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
            return SendResult(success=False, error=f"draft transport error: {e}")
        if result.get("success"):
            return SendResult(success=True)
        if result.get("ambiguous"):
            # Ack lost (transport timeout, returned rather than raised): same
            # contract as the except branch — keep interception armed.
            #
            # RAW BODY PRESERVED. Dropping it made `declined_send` fall through
            # to the error-text branch, and an ambiguous result whose text
            # happens to carry the decline marker ("... egress declined: ack
            # lost") then read as a DEFINITE refusal and terminated the run.
            # Ambiguous means the frame may well have been delivered, so it is
            # a transport outcome, never an authorization one.
            return SendResult(
                success=False,
                error=str(result.get("error") or "draft ack lost"),
                raw_response=result,
            )
        # DEFINITE connector rejection: disarm. The stream consumer falls back to
        # edit-based streaming and its turn-final must go out as a REAL send, not a
        # seal on a stream the connector just declared unusable.
        if self._open_draft_by_chat.get(chat_key) == draft_id:
            self._open_draft_by_chat.pop(chat_key, None)
        # P5(b): carry the structured body. The stream consumer reads a bare
        # draft failure as "draft transport unusable", disables drafts, and
        # falls through to a plain send — a second op against the chat the
        # connector just refused. Verified end to end with the real
        # GatewayStreamConsumer: ops were ['draft', 'send'].
        if is_egress_decline(result):
            log_decline("draft", chat_id, result)
        return SendResult(
            success=False,
            error=str(result.get("error") or decline_error(result) or "draft failed"),
            raw_response=result,
        )


    async def _seal_open_draft(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[dict[str, Any]],
        *,
        draft_key: Optional[str] = None,
    ) -> SendResult:
        """Convert the turn-final send into the sealing draft frame."""
        from gateway.relay.adapter import (SendResult, asyncio, decline_error, is_egress_decline, log_decline, logger)
        if draft_key is None:
            draft_key = self._draft_key(str(chat_id), metadata)
        draft_id = self._open_draft_by_chat.pop(draft_key)
        # Tombstone BEFORE the transport call: whatever the ack says, this draft_id
        # must never be re-armed by a straggler frame.
        self._sealed_draft_by_chat[draft_key] = draft_id
        self._evict_oldest(self._sealed_draft_by_chat)
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        seal_frame = self._draft_frame(chat_id, draft_id, content, True, metadata)

        _seal_platform = self._platform_by_chat.get(str(chat_id))
        _transport = self._transport  # narrowed by the None-guard above

        async def _attempt() -> Optional[dict[str, Any]]:
            """One seal attempt; None means ambiguous (exception or lost ack)."""
            try:
                r = await _transport.send_outbound(seal_frame, platform=_seal_platform)
            except Exception as e:
                logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
                logger.warning("relay seal transport error (ambiguous): %s", e)
                return None
            if r.get("ambiguous"):
                logger.warning("relay seal ack lost (ambiguous): %s", r.get("error"))
                return None
            return r

        # Ambiguous outcomes retry the SAME idempotent frame once: the connector's
        # sealed-key tombstone returns the original stream ts for a repeated final
        # and never opens a second stream. Two consecutive ack losses on one socket
        # almost always mean the transport is down. Cancellation safety: the open
        # entry was popped and the tombstone written BEFORE the await; restore both
        # before re-raising so the later abandon pass can still seal the stream.
        try:
            result = await _attempt()
            if result is None:
                result = await _attempt()
        except asyncio.CancelledError:
            self._open_draft_by_chat[draft_key] = draft_id
            if self._sealed_draft_by_chat.get(draft_key) == draft_id:
                self._sealed_draft_by_chat.pop(draft_key, None)
            raise
        if result is None:
            # Same ambiguity contract as send_draft: the retry's ack was lost,
            # so the seal may have been applied. Marked explicitly rather than
            # left to text inference.
            return SendResult(
                success=False,
                error="draft seal ambiguous after retry (transport ack lost)",
                raw_response={"success": False, "ambiguous": True},
            )
        if result.get("success"):
            # The connector returns the stream's ts as the message identity.
            return SendResult(success=True, message_id=str(result.get("message_id") or "") or None)
        # P5(b): carry the structured body. Without it the caller cannot tell a
        # lane failure (fall through to a plain send, correct) from an
        # AUTHORIZATION decline (a plain send re-delivers the very content the
        # connector refused, to the same chat).
        if is_egress_decline(result):
            log_decline("draft_seal", chat_id, result)
        return SendResult(
            success=False,
            error=str(result.get("error") or decline_error(result) or "draft seal failed"),
            raw_response=result,
        )
