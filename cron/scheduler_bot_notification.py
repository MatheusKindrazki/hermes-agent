"""Passive scheduled notifications to a canonical character conversation."""
from __future__ import annotations
import logging
import uuid
from typing import Optional

logger = logging.getLogger("cron.scheduler")

BOT_CHAT_NOTIFY_PLATFORM = "bot-chat-notify"

def parse_bot_chat_notify_deliver_token(part: str) -> Optional[str]:
    """Return the profile in a passive ``bot-chat-notify`` target."""
    raw = (part or "").strip()
    lowered = raw.lower()
    if lowered == BOT_CHAT_NOTIFY_PLATFORM:
        return ""
    prefix = BOT_CHAT_NOTIFY_PLATFORM + ":"
    if lowered.startswith(prefix):
        return raw[len(prefix):].strip()
    return None


def _deliver_to_bot_chat_notify(
    job: dict, content: str, profile: str
) -> Optional[str]:
    """Append scheduled output to a canonical Bot Chat without an agent turn.

    This is the passive counterpart to :func:`_deliver_to_bot_chat`.  It uses
    the exact title registry that Bot Mode itself uses, follows a compression
    continuation to its live tip, and writes one typed transcript row.  The
    normal SessionDB guards refuse a concurrent turn or compression instead
    of splicing into a moving transcript.
    """
    db = None
    try:
        if profile:
            from hermes_cli.profiles import get_profile_dir

            profile_home = get_profile_dir(profile)
        else:
            from hermes_constants import get_hermes_home

            profile_home = get_hermes_home()

        from hermes_state import SessionDB

        db = SessionDB(db_path=profile_home / "state.db")
        canonical = db.get_session_by_title("Bot Chat")
        if not canonical:
            return (
                "bot-chat-notify delivery failed: canonical Bot Chat does "
                f"not exist for profile '{profile or '(own)'}'"
            )
        session_id = db.get_compression_tip(str(canonical["id"])) or str(
            canonical["id"]
        )
        job_name = str(job.get("name") or job.get("id") or "cron job")
        fire_claim = job.get("fire_claim")
        fire_at = (
            str(fire_claim.get("at") or "")
            if isinstance(fire_claim, dict)
            else ""
        )
        delivery_id = "cron-notify:" + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "\0".join(
                    (
                        str(job.get("id") or ""),
                        fire_at or str(job.get("last_run_at") or "manual"),
                        content,
                    )
                ),
            )
        )
        if db.has_platform_message_id(session_id, delivery_id):
            logger.info(
                "Job '%s': passive Bot Chat delivery already persisted",
                job.get("id", "?"),
            )
            return None
        message = (
            f"[Cron delivery: {job_name}]\n"
            "Scheduled output; no agent turn was started.\n\n"
            f"{content}"
        )
        db.append_message(
            session_id=session_id,
            role="user",
            content=message,
            platform_message_id=delivery_id,
            display_kind="internal_notification",
            display_metadata={
                "source": "cron",
                "job_id": str(job.get("id") or ""),
                "passive": True,
            },
        )
        try:
            db.touch_session_activity(
                session_id,
                description=f"cron notification: {job_name}",
            )
        except Exception:
            logger.debug(
                "Job '%s': passive Bot Chat activity stamp failed",
                job.get("id", "?"),
                exc_info=True,
            )
        logger.info(
            "Job '%s': passively delivered to Bot Chat of profile '%s'",
            job.get("id", "?"),
            profile or "(own)",
        )
        return None
    except Exception as exc:
        logging.getLogger(__name__).debug('_deliver_to_bot_chat_notify: optional operation failed', exc_info=True)
        message = (
            f"bot-chat-notify delivery to profile '{profile or '(own)'}' "
            f"failed: {type(exc).__name__}: {str(exc) or 'unknown error'}"
        )
        logger.warning("Job '%s': %s", job.get("id", "?"), message)
        return message
    finally:
        if db is not None:
            db.close()
