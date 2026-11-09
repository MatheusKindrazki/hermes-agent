"""Deliver cron output to local inboxes, Bot Chats, and rooms."""

def deliver_nonplatform_target(job, content, cleaned_content, target, for_failure, errors, unverified):
    from cron.scheduler_delivery import (BOT_CHAT_NOTIFY_PLATFORM, BOT_CHAT_PLATFORM, ROOM_PLATFORM,
        _deliver_to_bot_chat_notify, _deliver_to_bot_chat, deliver_to_room)

    suppressed = 0
    if target["platform"] == BOT_CHAT_NOTIFY_PLATFORM:
        error = _deliver_to_bot_chat_notify(job, content, target["chat_id"])
        if error:
            errors.append(error)
        return True, suppressed
    if target["platform"] == BOT_CHAT_PLATFORM:
        bot_chat_error = _deliver_to_bot_chat(job, content, target["chat_id"], for_failure=for_failure)
        suppressed = job.pop("_notification_all_targets_suppressed", False)
        if bot_chat_error:
            receipt_target = f"bot-chat:{target['chat_id'] or '(own)'}"
            receipt = job.get("_bot_chat_delivery_receipts", {}).get(receipt_target)
            if not receipt or receipt["status"] not in ("queued", "claimed"):
                errors.append(bot_chat_error)
            if receipt and receipt["status"] == "ambiguous":
                unverified.append(bot_chat_error)
        return True, suppressed
    if target["platform"] == ROOM_PLATFORM:
        room_error = deliver_to_room(job, content, target["chat_id"], for_failure=for_failure)
        if room_error:
            errors.append(room_error)
        return True, suppressed
    if target["platform"] == "local":
        from gateway.session_cron import deliver_to_local_chat
        from hermes_constants import get_hermes_home
        local_error = deliver_to_local_chat(get_hermes_home(), target["chat_id"],
                                            cleaned_content, diagnostic=for_failure)
        if local_error:
            errors.append(local_error)
        return True, suppressed

    return False, 0
