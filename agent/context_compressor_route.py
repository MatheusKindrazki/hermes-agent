"""Per-attempt summary-route ownership and one-shot consumption."""
from __future__ import annotations
import contextlib
import contextvars
from typing import Any, Optional

# Summary-route pin lives in a ContextVar (not on the shared compressor) so the retry after a stalled
# summary sees it while the detached stalled worker does not. A stall raises nothing, so the aux client's
# exception-path fallback never fires; the host pins a fallback route for exactly ONE retry (the sole aux
# call per compaction). The main-model retry must NOT re-issue the pin.
# ── Pinned summary route ───────────────────────────────────────────────── The summary call normally
# resolves its provider/model from ``auxiliary.compression``. One caller needs to override that for a single
# attempt: after the host's progress-aware timeout aborts a stalled summary (#78981),
# ``agent.conversation_compression`` re-runs compression with the route pinned to a configured
# ``fallback_chain`` entry. Nothing raised out of the stalled call, so the auxiliary client's own fallback
# handling — which only runs from its exception path — never saw that failure. A ContextVar, not an
# attribute on the compressor: the aborted worker is detached and still alive on the pool, and the
# compressor object is shared with it. Context is copied per worker (``propagate_context_to_thread``), so
# the pin reaches the retry's whole synchronous call chain and cannot leak into the stalled attempt or any
# unrelated auxiliary call. Coverage is the single ``_generate_summary`` LLM call only. That is one call per
# compression run (its only non-recursive call site is the compress path; the two recursive calls are the
# deliberate main-model retry that must NOT re-issue the pin). The summary call is the ONLY auxiliary LLM
# call a lean compaction attempt makes (#96603) — there are no sibling digest calls.
_SUMMARY_ROUTE_PIN: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("hermes_summary_route_pin", default=None)
)

# ``timeout`` is included so a fallback entry keeps its own deadline.
_PINNED_ROUTE_FIELDS: tuple[str, ...] = ("provider", "model", "base_url", "api_key", "api_mode", "timeout")


@contextlib.contextmanager
def pin_summary_route(route: Optional[dict[str, Any]]):
    """Pin the next summary LLM call to an explicit route; ``None`` is a no-op. Re-entrant: restores the prior pin."""
    token = _SUMMARY_ROUTE_PIN.set(route if isinstance(route, dict) else None)
    try:
        yield
    finally:
        _SUMMARY_ROUTE_PIN.reset(token)


def take_pinned_summary_route() -> Optional[dict[str, Any]]:
    """Read and consume the pinned summary route (single use: the main-model retry must not re-issue it)."""
    route = _SUMMARY_ROUTE_PIN.get()
    if route is not None:
        _SUMMARY_ROUTE_PIN.set(None)
    return route


# Pinned route that names NO summary model: compress() skips the summary LLM and inserts its deterministic
# fallback summary instead (``abort_on_summary_failure`` still aborts). The host pins it when the summary
# route stalls again after a stall-class backoff already burned one idle window (#112420), so a provably
# unhealthy route degrades once instead of re-entering the same silent stream every turn.
DETERMINISTIC_SUMMARY_ROUTE: dict[str, Any] = {"label": "deterministic fallback summary", "deterministic": True}


def take_deterministic_summary_pin() -> bool:
    """Consume the pin when it is the deterministic sentinel; a real route (or no pin) is left in place."""
    route = _SUMMARY_ROUTE_PIN.get()
    if not (isinstance(route, dict) and route.get("deterministic") is True):
        return False
    _SUMMARY_ROUTE_PIN.set(None)
    return True


def _pinned_summary_call_kwargs() -> dict[str, Any]:
    """Consume the pinned route as explicit ``call_llm`` keyword arguments."""
    route = take_pinned_summary_route() or {}
    return {field: route[field] for field in _PINNED_ROUTE_FIELDS if route.get(field) not in (None, "")}
