"""Restart-safe cron result hand-off to the gateway-owned delivery queue."""
import os


def maybe_queue_external_delivery(job, content, targets, adapters, for_failure):
    from cron.scheduler_delivery import BOT_CHAT_PLATFORM, ROOM_PLATFORM, _record_delivery_verification

    # Restart-safe workers have no live gateway adapters: hand the send back through a durable
    # queue so the current or replacement gateway performs it with relay/E2EE parity. The execution
    # id is the idempotency key (the queue never retries an uncertain claimed send). Match on THIS
    # job's own attempt: a worker's script may dispatch another job in-process (`hermes cron run`),
    # and that nested delivery must not be keyed under the outer execution id.
    external_execution = os.environ.get("_HERMES_CRON_EXTERNAL_WORKER", "")
    if (external_execution and adapters is None
            and external_execution == str(job.get("execution_id") or "")
            and any(target["platform"] not in (BOT_CHAT_PLATFORM, ROOM_PLATFORM) for target in targets)):
        from cron.delivery_queue import enqueue_and_wait

        _record_delivery_verification(job, [])
        error = enqueue_and_wait(external_execution, job, content, for_failure=for_failure)
        from cron.delivery_queue import get_status
        delivery_status = get_status(external_execution)
        if delivery_status and delivery_status["status"] == "suppressed":
            job["_notification_all_targets_suppressed"] = True
        from cron.jobs import get_job
        refreshed = get_job(job["id"]) or {}
        job["last_delivery_queued"] = refreshed.get("last_delivery_queued")
        return True, error

    return False, None
