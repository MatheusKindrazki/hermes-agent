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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils import is_truthy_value


@dataclass(frozen=True)
class VoiceTurnPolicy:
    skip_context_files: bool = False
    skip_background_review: bool = False


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
        skip_background_review=is_truthy_value(policy.get("skip_background_review")))
