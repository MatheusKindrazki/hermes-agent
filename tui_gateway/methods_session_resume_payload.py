"""The common ``session.resume`` payload (``methods_session`` split).

Moved from ``tui_gateway/methods_session.py`` (file-line ratchet) when the payload gained ``compactions``
(RFC-001 V1). The body closes over server.py globals through ``method_ctx.bind_module`` exactly as before;
publication runs from the parent's ``register()``.
"""

from __future__ import annotations

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()


def _resume_response(
    ctx: _Resume, sid: str, record: dict, *, info: dict, display: list = (), count_source: list | None = None,
    messages: list | None = None, message_count: int | None = None, running: bool = False,
    status: str = "idle", hydrating: bool | None = None, started_at=None, auto_continue=None,
) -> dict:
    """Common resume payload; omit_messages counts ``count_source`` (client still learns the stored size)."""
    from .resume_compactions import stored_compactions
    if messages is None:
        messages = ctx.messages(display)
    if message_count is None:
        message_count = len(count_source) if ctx.omit_messages else len(messages)
    payload = {"session_id": sid, "resumed": ctx.target, "message_count": message_count, "messages": messages,
               **({"messages_omitted": ctx.omit_messages} if hydrating is None else {"hydrating": hydrating}),
               "info": info, "inflight": None, "running": running, "session_key": ctx.target,
               "started_at": record["created_at"] if started_at is None else started_at, "status": status,
               # RFC-001 V1 (the voice rotates on it). A deferred resume handed ctx.db to its worker: unknown.
               "compactions": None if hydrating else stored_compactions(ctx.db, ctx.target)}
    if auto_continue is not None:
        payload["auto_continue"] = auto_continue
    return _ok(ctx.rid, _attach_todo_state(payload, record))


def register(server) -> None:
    """Publish this module's helpers onto ``server`` (rebound to its globals)."""
    bind_module(globals(), server, skip=("_",))
