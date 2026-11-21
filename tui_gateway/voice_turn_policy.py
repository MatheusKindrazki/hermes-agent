"""Per-source agent build policy for voice surfaces (``voice.turn_policy`` in config.yaml).

A spoken turn from a phone or a watch pays for the full desktop agent: the profile's AGENTS.md
lands in the prompt, and the end-of-turn background review forks a second model call that can
patch skills from a voice transcript. The policy lets a deployment trim both for the session
sources it names. Everything is off unless config opts in::

    voice:
      turn_policy:
        sources: [iphone-voice, iphone-notebook, watch]
        skip_context_files: true
        skip_background_review: true
        toolsets: [web, vision, memory, session_search, clarify, mcp-front-memory]   # optional
        max_iterations: 6                                                           # optional

``toolsets`` replaces the CLI toolset list for those sessions (the client surface fold-in and
``agent.disabled_toolsets`` still apply): a voice turn re-sends every visible schema on every API
call, and terminal/file/browser/kanban/skills tools are never used by voice but cost about 20k
tokens per call. Without ``skills`` the skill index leaves the prompt too. ``max_iterations``
caps the API calls of one voice turn; at the cap Hermes asks the model for one toolless summary,
so the owner hears an answer instead of waiting on a seventh tool round.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils import is_truthy_value


@dataclass(frozen=True)
class VoiceTurnPolicy:
    skip_context_files: bool = False
    skip_background_review: bool = False
    toolsets: tuple[str, ...] | None = None  # None = the CLI toolsets, unchanged
    max_iterations: int | None = None  # None = the configured agent.max_turns


_OFF = VoiceTurnPolicy()


def _policy_section(cfg: Any) -> dict:
    voice = cfg.get("voice") if isinstance(cfg, dict) else None
    policy = voice.get("turn_policy") if isinstance(voice, dict) else None
    return policy if isinstance(policy, dict) else {}


def resolve_voice_turn_policy(cfg: Any, source: str | None) -> VoiceTurnPolicy:
    """The policy for a session whose DB ``source`` is ``source``; all-off when the source is not listed."""
    policy = _policy_section(cfg)
    sources = policy.get("sources")
    if not source or not isinstance(sources, (list, tuple)):
        return _OFF
    if source.strip().lower() not in {str(s).strip().lower() for s in sources}:
        return _OFF
    return VoiceTurnPolicy(
        skip_context_files=is_truthy_value(policy.get("skip_context_files")),
        skip_background_review=is_truthy_value(policy.get("skip_background_review")),
        toolsets=_toolsets(policy.get("toolsets")),
        max_iterations=_max_iterations(policy.get("max_iterations")))


def _toolsets(raw: Any) -> tuple[str, ...] | None:
    """A non-empty list of toolset names, or None (a scalar, an empty list or junk keeps the CLI list:
    an empty voice toolset would silently leave the voice with no tools at all)."""
    if not isinstance(raw, (list, tuple)):
        return None
    names = tuple(dict.fromkeys(str(n).strip() for n in raw if str(n).strip()))
    return names or None


def _max_iterations(raw: Any) -> int | None:
    """An integer from 2 to 50, else None (one call cannot both use a tool and answer)."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 2 <= value <= 50 else None
