"""Optional durable queue hand-off for outgoing Bot Chat messages."""
from pathlib import Path
import logging


def prepare_queued_delivery(argv, content, receipt, agent, dm_file, command, idempotency):
    from tools.bot_mode_dm import (_agent_home, _hermes_root, _update_delivery_receipt_inner,
        _update_delivery_receipt, _unlink_dm_file, _delivery_envelope_path, _err)

    from tools import bot_delivery_queue as queue
    source_home = Path(_agent_home(agent))
    if queue.local_target(argv) and queue.enabled(source_home):
        try:
            store = queue.Queue(_hermes_root(source_home))
            job = store.enqueue(argv, content, receipt, source_home)
            if idempotency is not None:
                # Enforced/observed callers already own this exact ledger
                # lock until the deferred spawn tuple has been prepared.
                _update_delivery_receipt_inner(dm_file, job["state"])
            else:
                _update_delivery_receipt(dm_file, job["state"])
            receipt = {**receipt, "state": job["state"], "queue_id": job["id"]}
            command = queue.wait_command(store.root, job["id"])
            _unlink_dm_file(dm_file)
            _unlink_dm_file(dm_file + ".receipt.json")
            _unlink_dm_file(str(_delivery_envelope_path(dm_file)))
            dm_file = None  # the queue now owns its private payload copy
        except Exception as exc:
            logging.getLogger(__name__).debug('_start_delivery_inner: optional operation failed', exc_info=True)
            return _err(f"Durable delivery could not be queued: {type(exc).__name__}: {exc}")
    return command, dm_file, receipt
