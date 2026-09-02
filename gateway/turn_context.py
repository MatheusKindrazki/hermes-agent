"""Per-turn context shared between ``GatewayRunner._run_agent_inner`` and the
``TurnRunner`` collaborator (gateway/run.py).

``_run_agent_inner`` historically defined its tool-progress plumbing as nested
closures (``progress_callback`` ~250 LOC, ``send_progress_messages`` ~353 LOC)
that closed over ~20 enclosing locals.  ``TurnContext`` is the extraction seam:
each closed-over local becomes a field on this dataclass, so the closure bodies
can move onto ``TurnRunner`` methods unchanged modulo ``name`` -> ``ctx.name``
rewrites.

Field notes:

- All fields are written once by ``_run_agent_inner`` while wiring up the turn
  (a few — ``_progress_metadata``, ``_progress_reply_to``, ``agent_holder`` —
  are computed slightly later than construction and assigned onto the ctx as
  soon as the original locals were bound).  None of the original closures
  *rebound* their captured names (no ``nonlocal``); mutable state uses the
  same single-element-list containers as before (``last_progress_msg``,
  ``repeat_count``, ...), so mutation stays visible to the outer body through
  the shared objects exactly as it did through the shared closure cells.
- ``_run_still_current`` stays a callable (it captures ``self``/
  ``session_key``/``run_generation``); carrying the callable keeps the
  extracted bodies byte-identical.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional


@dataclass(frozen=True)
class RequestContext:
    """Immutable authority snapshot for one request/turn."""

    profile: str
    tenant: str
    hermes_home: Path
    workspace: Path
    model: str
    provider: str
    approval: str
    session_id: str
    secret_scope_bound: bool


_REQUEST_CONTEXT: ContextVar[Optional[RequestContext]] = ContextVar(
    "HERMES_REQUEST_CONTEXT_V2", default=None
)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def request_context_v2_enabled(config: Optional[dict] = None) -> bool:
    """Read the config.yaml gate; absent or malformed remains default-off."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    gateway = _mapping(_mapping(config).get("gateway"))
    reliability = _mapping(gateway.get("reliability"))
    request_context = _mapping(reliability.get("request_context"))
    value = request_context.get("enabled", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def compose_request_context(
    *,
    profile: str,
    tenant: str,
    model: str,
    provider: str,
    approval: str,
    session_id: str,
) -> RequestContext:
    """Compose one request from existing home/cwd/secret authority seams."""
    from agent.runtime_cwd import resolve_agent_cwd
    from agent.secret_scope import current_secret_scope
    from hermes_constants import get_hermes_home

    identity = {
        "profile": profile,
        "tenant": tenant,
        "model": model,
        "provider": provider,
        "approval": approval,
        "session_id": session_id,
    }
    missing = [
        key
        for key, value in identity.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise ValueError(
            "request context requires non-empty " + ", ".join(sorted(missing))
        )
    return RequestContext(
        profile=profile.strip(),
        tenant=tenant.strip(),
        hermes_home=get_hermes_home(),
        workspace=resolve_agent_cwd(),
        model=model.strip(),
        provider=provider.strip(),
        approval=approval.strip(),
        session_id=session_id.strip(),
        secret_scope_bound=current_secret_scope() is not None,
    )


def current_request_context() -> Optional[RequestContext]:
    return _REQUEST_CONTEXT.get()


@contextmanager
def request_context_scope(
    request_context: RequestContext,
    *,
    config: Optional[dict] = None,
):
    """Publish the v2 context only when the config.yaml gate is enabled."""
    if not request_context_v2_enabled(config):
        yield None
        return
    token = _REQUEST_CONTEXT.set(request_context)
    try:
        yield request_context
    finally:
        _REQUEST_CONTEXT.reset(token)


@dataclass
class TurnContext:
    """Closed-over locals of ``_run_agent_inner`` needed by ``TurnRunner``."""

    # --- read-only turn identity / wiring -------------------------------
    source: Any = None
    request_context: Optional[RequestContext] = None
    _run_still_current: Callable[[], bool] = None  # type: ignore[assignment]
    _live_status_adapter: Any = None
    _live_status_mode: str = "off"
    _thinking_enabled: bool = False
    progress_mode: str = "off"
    progress_grouping: str = "grouped"
    tool_progress_enabled: bool = False

    # --- queues ----------------------------------------------------------
    progress_queue: Any = None
    log_queue: Any = None

    # --- mutable single-element containers (shared with the outer body) --
    last_progress_msg: list = field(default_factory=lambda: [None])
    last_tool: list = field(default_factory=lambda: [None])
    last_was_terminal_block: list = field(default_factory=lambda: [False])
    repeat_count: list = field(default_factory=lambda: [0])
    long_tool_hint_fired: list = field(default_factory=lambda: [False])
    agent_holder: list = field(default_factory=lambda: [None])

    # --- constants / cleanup bookkeeping ---------------------------------
    _LONG_TOOL_THRESHOLD_S: float = 30.0
    _cleanup_progress: bool = False
    _cleanup_msg_ids: List[str] = field(default_factory=list)

    # --- progress threading metadata (assigned after construction, before
    #     send_progress_messages is scheduled) ----------------------------
    _progress_metadata: Optional[dict] = None
    _progress_reply_to: Optional[Any] = None

    # ------------------------------------------------------------------
    # run_sync extraction (second wave of the seam): the closed-over locals
    # of ``_run_agent_inner`` that ``run_sync`` (and the four sibling bridge
    # callbacks) captured.  Same rules as above: read-only snapshots or
    # shared mutable containers; ``message`` is the ONE exception — the old
    # closure rebound it via ``nonlocal``, so the rebind sites now write
    # ``ctx.message`` and the outer body reads ``ctx.message`` afterwards.
    # ------------------------------------------------------------------

    # --- the ex-``nonlocal`` turn message (rebindable) --------------------
    message: Optional[str] = None

    # --- turn parameters / config snapshots (read-only in run_sync) -------
    history: Any = None
    context_prompt: Optional[str] = None
    channel_prompt: Optional[str] = None
    session_id: Optional[str] = None
    session_key: Optional[str] = None
    run_generation: Optional[int] = None
    process_task_id: str = ""
    process_baseline: frozenset[str] = field(default_factory=frozenset)
    _interrupt_depth: int = 0
    event_message_id: Optional[str] = None
    moa_config: Optional[dict] = None
    persist_user_message: Optional[Any] = None
    persist_user_timestamp: Optional[float] = None
    # display_kind stamped on the persisted user row at turn start when this
    # turn was self-injected (MessageEvent.internal), e.g.
    # "internal_notification" for async-delegation/background notifications
    # (#82888). DB-only presentation metadata; never sent to the provider.
    persist_user_display_kind: Optional[str] = None
    user_config: Any = None
    enabled_toolsets: Any = None
    disabled_toolsets: Any = None
    log_mode_enabled: bool = False
    interim_assistant_messages_enabled: bool = False
    needs_progress_queue: bool = False

    # --- durable admission (K8) -------------------------------------------
    # The ONE-SHOT carrier for the admission the gateway ingress recorded for
    # this request, captured in the async parent and transported explicitly to
    # the single run_conversation it belongs to.
    #
    # It is carried here, as an object, rather than read from a ContextVar in
    # the worker: ``_run_in_executor_with_context`` runs turn work under
    # ``copy_context()``, so a ContextVar cleared inside that copy does not
    # clear the parent's, and a second executor call in the same request would
    # re-read an admission that was already spent. The box is shared by
    # reference, so taking it is visible to the parent that owns the lifecycle
    # and retires it.
    k8_pre_admission: Any = None

    # --- lazy-imported callables captured from the outer body -------------
    AIAgent: Any = None
    resolve_display_setting: Any = None

    # --- mutable holder cells (shared-list pattern; outer body + the
    #     post-executor closures read mutations through the same objects) --
    result_holder: list = field(default_factory=lambda: [None])
    tools_holder: list = field(default_factory=lambda: [None])
    stream_consumer_holder: list = field(default_factory=lambda: [None])
    streaming_tts_consumer_holder: list = field(default_factory=lambda: [None])

    # --- voice-ack wiring --------------------------------------------------
    _voice_ack_fired: list = field(default_factory=lambda: [False])
    _voice_ack_guild: list = field(default_factory=lambda: [None])
    _voice_ack_loop: Any = None

    # --- hook / status bridge wiring (published at original binding sites) -
    _loop_for_step: Any = None
    _hooks_ref: Any = None
    _status_adapter: Any = None
    _status_chat_id: Any = None
    _status_thread_metadata: Optional[dict] = None

    # --- extracted sibling callbacks (bound TurnRunner methods; run_sync
    #     reads them through the ctx exactly where it used to close over
    #     the sibling closures) ---------------------------------------------
    progress_callback: Optional[Callable] = None
    voice_ack_callback: Optional[Callable] = None
    _step_callback_sync: Optional[Callable] = None
    _event_callback_sync: Optional[Callable] = None
    _status_callback_sync: Optional[Callable] = None

    # --- Slack-native task-card progress (opt-in; #29483) ------------------
    # True when the Slack adapter's ``native_task_cards_enabled()`` opt-in is
    # set for this turn's platform. The ID-bearing lifecycle callbacks are
    # published by TurnRunner (like voice_ack_callback above) so tool starts
    # and completions correlate by real tool-call ID instead of tool name.
    _native_slack_task_cards: bool = False
    native_tool_start_callback: Optional[Callable] = None
    native_tool_complete_callback: Optional[Callable] = None
