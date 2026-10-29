"""APIServerExecutionMixin owns one bounded execution phase of APIServerAdapter."""
from __future__ import annotations
from typing import Any, Optional, TYPE_CHECKING


class APIServerExecutionMixin:
    async def _run_agent(
        self, user_message: str, conversation_history: list[dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None, session_id: Optional[str] = None,
        stream_delta_callback=None, tool_progress_callback=None, tool_start_callback=None,
        tool_complete_callback=None, interim_assistant_callback=None, reasoning_callback=None,
        status_callback=None, agent_ref: Optional[list] = None, active_run_id: Optional[str] = None,
        gateway_session_key: Optional[str] = None, requested_model: Optional[str] = None,
        requested_provider: Optional[str] = None, model_options: Optional[dict[str, Any]] = None,
        route: Optional[dict[str, Any]] = None, session_model: Optional[str] = None,
        requested_runtime: Optional[dict[str, Any]] = None, route_source: str = "global",
        confirmed_runtime_lock: bool = False, bind_declared_conversation: bool = False,
        request_id: Optional[str] = None, history_from_session: bool = False, run_owner_scope=None,
        session_history_delivery: str = "", turn_author: Optional[dict[str, Any]] = None,
        relay_metadata: Optional[dict[str, Any]] = None, notification_category: str = "result",
        resume_unanswered_turn: bool = False, approval_notify_callback=None,
        approval_session_key: Optional[str] = None, walked_from: Optional[str] = None, delivery_metadata: Optional[dict[str, Any]] = None) -> tuple:
        """Create an agent and run one turn in a thread executor -> ``(result, usage)``.
        ``approval_notify_callback`` (with ``approval_session_key``) routes dangerous-command
        approval requests to the caller's stream, keyed like ``/v1/runs`` approvals (#51871).
        ``agent_ref[0]`` receives the agent so SSE writers can interrupt it; ``active_run_id``
        registers it in ``_active_run_agents``. Under a confirmed model lock the actual
        provider/model must match or the turn fails; ``runtime`` metadata is attached.
        ``session_history_delivery`` declares #98619 session-id provenance and default-denies: only audited
        producers whose client can address the id again pass "1" (see
        ``_bind_api_server_session``).
        ``turn_author`` only labels the turn for memory attribution. It grants nothing.
        ``resume_unanswered_turn`` marks a policy-gated re-run of a turn whose user row the failed attempt
        already persisted: the transcript's unanswered tail row is adopted from ``conversation_history``
        as THIS turn's user message instead of being appended a second time
        (``agent.session_persistence.adopt_unanswered_turn``; #115325)."""
        from gateway.platforms.api_server import (_ProviderAuthResolutionError, _api_request_browser_control_principal, _api_request_browser_control_transport_family, _api_request_profile, _api_runs, _clear_turn_process_ownership, _publish_turn_process_ownership, asyncio, logger, suppress, time, uuid)
        if getattr(self.gateway_runner, "session_authority", None) is not None:
            from gateway.session_api_turn import run_api_turn
            return await run_api_turn(self, user_message=user_message, conversation_history=conversation_history,
                ephemeral_system_prompt=ephemeral_system_prompt, session_id=session_id,
                stream_delta_callback=stream_delta_callback, tool_progress_callback=tool_progress_callback,
                tool_start_callback=tool_start_callback, tool_complete_callback=tool_complete_callback,
                interim_assistant_callback=interim_assistant_callback, reasoning_callback=reasoning_callback,
                status_callback=status_callback, walked_from=walked_from,
                agent_ref=agent_ref, active_run_id=active_run_id, gateway_session_key=gateway_session_key,
                requested_model=requested_model, requested_provider=requested_provider, model_options=model_options,
                route=route, session_model=session_model, requested_runtime=requested_runtime,
                route_source=route_source, confirmed_runtime_lock=confirmed_runtime_lock,
                bind_declared_conversation=bind_declared_conversation, request_id=request_id, run_owner_scope=run_owner_scope,
                history_from_session=history_from_session, session_history_delivery=session_history_delivery,
                turn_author=turn_author, resume_unanswered_turn=resume_unanswered_turn, relay_metadata=relay_metadata,
                approval_notify_callback=approval_notify_callback, approval_session_key=approval_session_key)
        loop = asyncio.get_running_loop()
        # ContextVars do not follow run_in_executor threads: capture here, re-enter in _run().
        request_profile = _api_request_profile.get()
        request_browser_control_principal = _api_request_browser_control_principal.get()
        request_browser_control_transport_family = _api_request_browser_control_transport_family.get()

        def _run():
            from gateway.session_context import clear_session_vars
            with self._profile_scope(request_profile):
                tokens = self._bind_api_server_session(
                    chat_id=session_id or "", session_key=gateway_session_key or session_id or "",
                    session_id=session_id or "", profile=request_profile or "",
                    browser_control_principal=request_browser_control_principal,
                    browser_control_transport_family=request_browser_control_transport_family,
                    session_history_delivery=session_history_delivery)
                agent = None
                from agent.notification_presentation import notification_turn
                from gateway.warning_notifications import diagnostic_turn_muted
                muted = diagnostic_turn_muted({"notification_category": notification_category}, "api_server")
                try:
                    agent = self._create_agent(
                        ephemeral_system_prompt=ephemeral_system_prompt, session_id=session_id,
                        stream_delta_callback=stream_delta_callback, tool_progress_callback=tool_progress_callback,
                        tool_start_callback=tool_start_callback, tool_complete_callback=tool_complete_callback,
                        interim_assistant_callback=interim_assistant_callback,
                        reasoning_callback=reasoning_callback, status_callback=status_callback,
                        gateway_session_key=gateway_session_key, requested_model=requested_model,
                        requested_provider=requested_provider, model_options=model_options, route=route,
                        session_model=session_model, confirmed_runtime_lock=confirmed_runtime_lock)
                    if agent_ref is not None:
                        agent_ref[0] = agent
                    if resume_unanswered_turn:
                        # A dispatcher's re-run of a failed delivery turn: the DM's own row is already
                        # in the store (the failed attempt persisted it at turn start), so continue THAT
                        # row instead of appending a second copy of the same text (#115325).
                        from agent.session_persistence import adopt_unanswered_turn

                        adopt_unanswered_turn(conversation_history, user_message, agent)
                    if active_run_id:
                        self._active_run_agents[active_run_id] = agent
                    effective_task_id = session_id or str(uuid.uuid4())
                    # Process baseline for disconnect reaping (this surface bypasses TurnRunner)
                    # + shutdown-interrupt registration, once for every caller.
                    # Baseline for selective background-process reaping on SSE client disconnect — mirrors
                    # gateway/run.py's gateway-turn cleanup (#76115); this API-server surface runs its own
                    # agent lifecycle and doesn't go through TurnRunner, so it needs its own baseline.
                    # /v1/runs runs its own agent lifecycle (no TurnRunner, no _run_agent) — record turn
                    # process ownership so stop/cancel can reap only the background processes this run
                    # created (#76115).
                    _publish_turn_process_ownership(agent, effective_task_id)
                    # Registering here, once, covers every _run_agent() caller — the same reason the
                    # _ProviderAuthResolutionError handler below lives here rather than in each route. Only
                    # two callers pass ``agent_ref``, and only /v1/runs has a run_id, so neither is a usable
                    # hook for the rest. See #63529.
                    self._shutdown_interruptible_agents[id(agent)] = agent
                    # Passed only when set: a human turn keeps today's call shape.
                    author_kwargs = {"turn_author": turn_author} if turn_author is not None else {}
                    conversation_kwargs = dict(
                        user_message=user_message,
                        conversation_history=conversation_history,
                        task_id=effective_task_id,
                        **author_kwargs,
                    )
                    if relay_metadata:
                        conversation_kwargs["relay_metadata"] = relay_metadata
                    if delivery_metadata:
                        conversation_kwargs.update(persist_user_display_kind="bot_delivery", persist_user_display_metadata=delivery_metadata)
                    approval_token = None
                    if approval_notify_callback is not None and approval_session_key:
                        # Same machinery as /v1/runs (_run_agent_sync): the contextvar scopes
                        # this turn's approvals to the key the resolve endpoint looks up.
                        from tools.approval import register_gateway_notify
                        from tools.approval_context import set_current_session_key
                        approval_token = set_current_session_key(approval_session_key)
                        register_gateway_notify(approval_session_key, approval_notify_callback)
                    try:
                        with notification_turn(agent, muted=muted, session_id=session_id or ""):
                            result = agent.run_conversation(**conversation_kwargs)
                    finally:
                        if approval_token is not None:
                            from tools.approval_context import reset_current_session_key
                            _api_runs._unregister_approval_notify(approval_session_key)
                            with suppress(Exception):
                                reset_current_session_key(approval_token)
                    result, usage = self._finish_turn_result(
                        agent, result, session_id, route=route, requested_runtime=requested_runtime,
                        route_source=route_source, confirmed_runtime_lock=confirmed_runtime_lock)
                    if muted and isinstance(result, dict):
                        # Project presentation only after finishing the source outcome. Keep
                        # the agent's result, transcript, failure flags and usage intact.
                        result = {**result, "_notification_presentation_suppressed": True}
                    return result, usage
                except _ProviderAuthResolutionError as exc:
                    # Typed provider-auth failure only, handled once for every caller in
                    # run.py's response shape (text, no HTTP error).
                    logger.warning("Provider resolution failed for session=%s: %s",
                                   session_id or "", exc)
                    return (
                        {"final_response": exc.user_text(), "messages": [],
                         "api_calls": 0, "tools": [],
                         **({"_notification_presentation_suppressed": True} if muted else {})},
                        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                except Exception as exc:
                    if muted:
                        # Keep the original exception/traceback for logs and failure
                        # handling; the HTTP/SSE boundary suppresses its presentation.
                        exc._notification_presentation_suppressed = True
                    raise
                finally:
                    # Turn over (any outcome): clear ownership so a late disconnect can't reap
                    # background work this turn deliberately left running.
                    if active_run_id:
                        self._active_run_agents.pop(active_run_id, None)
                    if agent is not None:
                        _clear_turn_process_ownership(agent)
                        self._shutdown_interruptible_agents.pop(id(agent), None)
                        self._memory_sessions.checkin(agent)
                        # Bind the declared key to the row the turn actually ended on
                        # (agent.session_id carries a mid-turn rotation). Opt-in per route.
                        # Record the declared conversation on the row the turn actually ended on —
                        # ``agent.session_id`` already carries a mid-turn compression rotation (#16938), so
                        # the next reply resolves the live transcript rather than its retired parent.
                        # Opt-in: only the routes that resolve their session id from the declared key
                        # (/v1/responses, /v1/runs) record one, so no other caller's rows change shape.
                        if bind_declared_conversation:
                            self._bind_declared_conversation(
                                getattr(agent, "session_id", None) or session_id, gateway_session_key)
                    clear_session_vars(tokens)
        self._activate_admitted_request()
        self._inflight_agent_runs += 1
        started_at = time.perf_counter()
        usage: Optional[dict[str, Any]] = None
        try:
# Worker-scoped count rides along so the shutdown close gate still sees the thread
            # after this handler task is cancelled (#116535); released in the worker's finally.
            result, usage = await _api_runs._submit_api_worker(loop, _run)
            return result, usage
        finally:
            self._inflight_agent_runs -= 1
            if usage is not None:
                self._record_api_metrics(usage, time.perf_counter() - started_at)
