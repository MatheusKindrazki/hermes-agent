"""Bot Mode agent-to-agent DM tool — ``message_agent``.

Lets a Bot Mode agent message a teammate (a profile on this install, an agent on
a registered peer gateway, or one on another Desktop-connected machine): the
target is validated against the live roster, the attribution prefix is applied
server-side, and the reply arrives later via the background-process completion
notification (fire-and-forget). Containment: the schema is injected ONLY into a
bot's canonical "Bot Chat" session on a Bot-Mode-managed install (same gate as
``tools/bot_mode_probe.py``; never in the registry or any toolset), and dispatch
re-checks that gate so a forged call returns a structured error. Transports:
local → ``hermes -p <name> chat --in ~ -c "Bot Chat" --create-if-missing -Q
--query-file <tmp>``; peer → ``hermes peer dm <peer>[/<name>] < <tmp>``; both via
``terminal_tool(background=True, notify_on_complete=True)``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

if __name__ == "__main__":
    # The delivery runner must use the same checkout as this script, including
    # when a worktree borrows an interpreter with another editable install.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback below
    fcntl = None

# Top-level imports stay stdlib-only: this module also runs directly as the background
# delivery runner (``python bot_mode_dm.py --run-delivery …``); Hermes helpers import lazily.

logger = logging.getLogger(__name__)

MESSAGE_AGENT_TOOL_NAME = "message_agent"

# Message body cap — generous for real work, small enough that a runaway paste can't
# turn one DM into a context bomb on the recipient.
MESSAGE_MAX_CHARS = 16000
# The delivery process's completion notification IS the reply: size it like a message, plus the
# runner's header and failure prose, instead of the 2000-char tail a build log gets.
REPLY_COMPLETION_CHARS = MESSAGE_MAX_CHARS + 2000
# A runner owns and removes each DM file; this bounds residual plaintext lifetime if
# the machine dies between spawn ack and the runner's finally.
_DM_DIR_NAME = "hermes-dm"
_DM_STALE_SECONDS = 24 * 60 * 60
_LIVE_WAIT_SECONDS = 300
# A live owner may run a turn past one wait window, but each waiting runner holds ~46 MB and the
# sender's notify slot: past this the runner reports pending (do not resend) and exits.
_LIVE_WAIT_MAX_SECONDS = 30 * 60

# '<peer>/<agent>' — peer names are lowercase (``hermes peer`` normalizes them).
_PEER_TARGET_RE = re.compile(r"^([a-z0-9][a-z0-9_-]{0,63})/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})$")
# Same shape as ``tools.bot_relay._HANDLE_RE`` (kept local: see import note above).
_LOCAL_TARGET_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
DELIVERY_ACK_SCHEMA = "hermes.delivery-ack.v1"
DELIVERY_ENVELOPE_SCHEMA = "hermes.delivery-envelope.v1"
_DELIVERY_QUERY_PREFIX = "HERMES_DELIVERY_V1\n"


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sha256(document: Any) -> str:
    return hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _encode_delivery_query(envelope: Mapping[str, Any], message: str) -> str:
    return _DELIVERY_QUERY_PREFIX + _canonical({"delivery": dict(envelope), "message": message})


def _public_delivery_receipt(record: dict[str, Any]) -> dict[str, Any]:
    hidden = {"turn_identity", "fence", "adapter_ack", "observation", "release_event_id", "release_state", "release_receipt"}
    return {key: value for key, value in record.items() if key not in hidden}


def _hermes_root(home: Path) -> Path:
    from tools.bot_mode_probe import _hermes_root as resolve
    return resolve(home)
# Fan-out bound: one combined runner holds one sub-runner per target.
FAN_OUT_MAX = 8


def _default_home() -> str:
    from hermes_constants import get_process_hermes_home
    return str(get_process_hermes_home())


def message_agent_tool_schema() -> dict:
    """OpenAI-format schema for ``message_agent`` (injected, not registered)."""
    return {
        "type": "function",
        "function": {
            "name": MESSAGE_AGENT_TOOL_NAME,
            "description": (
                "Send a message to ANOTHER agent (teammate) on this install, a registered peer gateway, or "
                "another connected machine. FIRE-AND-FORGET: it validates the target against the live roster, "
                "delivers into that agent's Bot Chat with your attribution prefixed, and returns a dispatch "
                "ack at once (status queued + delivery_id: a hand-off, not a receipt). It never returns the "
                "reply: finish your turn; the completion notification wakes you with the outcome (its "
                "`outcome` line says who answered or why not) — unless the ack says reply_delivery=\"poll\": "
                "then follow its process(action=\"wait\") instruction first. COMPOSE the message yourself "
                "(lead with the point and the concrete ask); never paste the user's words verbatim or share "
                "private 1:1 content. Targets: a roster name ('researcher'), '<peer>/<agent>' or '<peer>' for "
                "a peer gateway, '<handle>@<connection>' when a handle exists on several machines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (f"Who to message ('hermes' is the default agent). Up to {FAN_OUT_MAX} "
                                        "comma-separated names fan out: one combined reply."),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The message YOU composed for that agent (max "
                            f"{MESSAGE_MAX_CHARS} chars). Do not include the "
                            "'Message from …' prefix — it is added automatically."
                        ),
                    },
                    "constraints": {
                        "type": "string",
                        "description": "Limits their answer must respect (scope, length, format).",
                    },
                    "expecting": {
                        "type": "string",
                        "description": "What you need back from them; shown to them as the reply they owe you.",
                    },
                    "expects_reply": {
                        "type": "boolean",
                        "description": "false: notify only — no reply comes back and nothing wakes you.",
                    },
                    "cancel": {
                        "type": "string",
                        "description": "A delivery_id from your own ack: withdraw that queued request or stop the turn it started.",
                    },
                },
                "required": ["target"],
            },
        },
    }


def message_agent_authorized(agent: Any) -> bool:
    """The ``message_agent`` gate: a protocol-enabled agent whose session is a managed
    Bot-Mode canonical Bot Chat. Session-stable, so it is prompt-cache safe to re-evaluate
    on every tool-snapshot rebuild. Never raises."""
    try:
        if not getattr(agent, "_bot_mode_protocol", True):
            return False
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        # Managed-install check, NOT section non-emptiness: a SOUL.md carrying the
        # legacy protocol text gets an empty section but must still get the tool.
        return _session_title(agent) == BOT_CHAT_TITLE and is_bot_mode_managed(_agent_home(agent))
    except Exception:  # pragma: no cover — must never break a turn
        logger.debug("message_agent_authorized failed", exc_info=True)
        return False


message_agent_tool_enabled = message_agent_authorized


def ensure_message_agent_tool(agent: Any) -> bool:
    """Inject the ``message_agent`` schema into a Bot Chat agent's tool list (once per turn).
    Idempotent and deterministic for the session's life (the gate is stable from the
    first turn), so the tool list is byte-identical across turns — prompt-cache safe. Never raises."""
    try:
        if not getattr(agent, "_bot_mode_protocol", True):
            return False
        tools = getattr(agent, "tools", None)
        present = bool(tools) and any(
            isinstance(t, dict) and t.get("function", {}).get("name") == MESSAGE_AGENT_TOOL_NAME
            for t in tools
        )
        if not present:
            if not message_agent_authorized(agent):
                return False
            if agent.tools is None:
                agent.tools = []
            agent.tools.append(message_agent_tool_schema())
        # Success means BOTH halves hold: a tool-surface rebuild (compaction, MCP refresh)
        # can keep the schema while valid_tool_names is republished without it, and an
        # advertised-but-nondispatchable tool sends the model hunting for shellouts (#96105).
        valid = getattr(agent, "valid_tool_names", None)
        if isinstance(valid, set):
            valid.add(MESSAGE_AGENT_TOOL_NAME)
        return True
    except Exception:  # pragma: no cover — must never break a turn
        logger.debug("ensure_message_agent_tool failed", exc_info=True)
        return False


def _resolve_local_name(target: str, roster: list[str], root: Path | None = None) -> Optional[str]:
    """Map a target to a local profile FOLDER id: 'hermes' → 'default' unless a real 'hermes'
    profile holds that handle (``_profile_for_handle``); an exact folder id
    (case-insensitive); else — when ``root`` is given — a friendly name or its Desktop @-slug
    (profile.yaml ``display_name`` / Bot Mode title: 'Scribe', '@scribe', 'Dr. Foo' → 'foo').
    Ambiguous friendly names resolve to None so a DM never lands on the wrong bot (#100671)."""
    want = target.strip().lower()
    if not want:
        return None
    from tools.bot_mode_probe import _profile_for_handle

    want = _profile_for_handle(want, roster)
    exact = next((name for name in roster if name.lower() == want), None)
    if exact is not None or root is None:
        return exact
    from tools.bot_mode_probe import alias_forms, local_alias_map

    aliases = local_alias_map(root)
    hits = set().union(*(aliases.get(form, set()) for form in alias_forms(want) | {want}))
    return next(iter(hits)) if len(hits) == 1 else None



def _relay_enforce_refusal() -> Optional[str]:
    """Desktop relay has enqueue only, so enforced delivery cannot use it."""
    try:
        from agent.durable_admission import (
            admission_enabled,
            revalidate_current_turn_identity,
        )
        if not admission_enabled():
            return None
        revalidate_current_turn_identity()
    except Exception as exc:
        logging.getLogger(__name__).debug('_relay_enforce_refusal: optional operation failed', exc_info=True)
        return _err(f"Desktop relay authority refused: {type(exc).__name__}: {exc}")
    return _err(
        "Desktop relay has no durable destination ACK; delivery is disabled under enforce."
    )


# ── the tool ─────────────────────────────────────────────────────────────────


def _err(message: str, *, roster: list[str] | None = None, peers: list[str] | None = None) -> str:
    from tools.bot_failure_reasons import classify_agent_error

    payload: dict[str, Any] = {"error": message, "reason": classify_agent_error(message)}
    if roster is not None:
        payload["teammates"] = roster
    if peers is not None:
        payload["peers"] = peers
    return json.dumps(payload)


@dataclass(frozen=True)
class Sender:
    """The calling bot and its live roster, resolved once per ``message_agent`` call."""

    home: str
    root: Path
    me: str
    roster_homes: dict
    peers: list
    teammates: list
    task_id: Optional[str]
    agent: Any
    # The chain gate's author-envelope fields for this call (``tools/bot_mode_dm_chain.py``).
    chain: dict = field(default_factory=dict)
    origin_session_id: Optional[str] = None

    def roster_err(self, msg: str) -> str:
        return _err(msg, roster=self.teammates, peers=self.peers)


def _envelope(message: Any, constraints: Any, expecting: Any) -> str:
    """The request as the teammate reads it: the message, then its typed constraints and the reply
    the sender needs back, each on its own labelled line."""
    typed = [f"{label}: {str(value).strip()}" for label, value in (("Constraints", constraints),
                                                                  ("Reply needed", expecting)) if str(value or "").strip()]
    return "\n\n".join([str(message or "").strip(), *(["\n".join(typed)] if typed else [])])


def _wants_reply(value: Any) -> bool:
    """``expects_reply`` as models send it: a bool, or its string spelling."""
    return str(value).strip().lower() not in ("false", "0", "no")


def message_agent_tool(target: str = "", message: str = "", task_id: Optional[str] = None, agent: Any = None,
                       expects_reply: Any = True, cancel: str = "", constraints: str = "", expecting: str = "",
                       origin_session_id: Optional[str] = None) -> str:
    """Deliver ``message`` to ``target``'s Bot Chat (several comma-separated targets fan out),
    or withdraw the caller's own request ``cancel``. Returns a JSON ack/error.
    ``agent`` is the calling AIAgent — used for the Bot Chat gate and sender identity."""
    if not getattr(agent, "_bot_mode_protocol", True):
        return _err("message_agent is disabled for this agent session. Do not retry.")
    home = _agent_home(agent)
    try:
        from tools.bot_mode_probe import (
            BOT_CHAT_TITLE, NO_REPLY_MARKER, _display_name, _handle, _hermes_root, _peers,
            _profile_name as _self_profile_name, _roster, is_bot_mode_managed,
        )

        if _session_title(agent) != BOT_CHAT_TITLE:
            return _err("message_agent is only available in a Bot Mode 'Bot Chat' session. "
                        "This session is not one; do not retry.")
        if not is_bot_mode_managed(home):
            return _err("This install is not Bot-Mode-managed (no bot roster); "
                        "message_agent is unavailable. Do not retry.")
    except Exception as exc:  # pragma: no cover — defensive
        logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
        return _err(f"Bot Mode gate check failed: {exc}")
    from hermes_cli.observability.shared_metrics_signals import record_feature_used
    record_feature_used("bot_mode", hermes_home=home)

    root, me = _hermes_root(Path(home)), _self_profile_name(Path(home))
    roster_homes = dict(_roster(root))
    peers = _peers(root)
    sender = Sender(home=home, root=root, me=me, roster_homes=roster_homes, peers=peers,
                    teammates=[_handle(n, list(roster_homes)) for n in roster_homes if n != me], task_id=task_id, agent=agent, origin_session_id=origin_session_id)
    if str(cancel or "").strip():
        from tools.bot_mode_dm_coordination import cancel_own_delivery

        return cancel_own_delivery(str(cancel).strip(), str(target or ""), sender)

    if not str(message or "").strip():
        return _err("message is required — compose what you want to say to that agent.")
    body = _envelope(message, constraints, expecting)
    if len(body) > MESSAGE_MAX_CHARS:
        return _err(f"message too long ({len(body)} chars > {MESSAGE_MAX_CHARS}). "
                    "Send the essentials; share large content as a file path instead.")

    from tools.bot_mode_dm_coordination import split_targets

    targets = split_targets(target, sender)
    if not targets:
        return sender.roster_err("target is required.")
    if len(targets) > FAN_OUT_MAX:
        return _err(f"Too many targets ({len(targets)} > {FAN_OUT_MAX}); message the ones the task needs.")
    reply = _wants_reply(expects_reply)
    # Sender signature: the friendly name when the bot has one (#89720); the @handle stays the routing alias.
    signature = f"Message from 🤖 {_display_name(me, roster_homes.get(me, Path(home)), list(roster_homes))} (@{_handle(me, list(roster_homes))}): "
    from tools.bot_mode_dm_chain import send_gated

    # The runtime's chain rules run before any transport (hop cap, repeated ask, reply to a bot: #137027).
    return send_gated(targets, reply, body, lambda wants: signature + ("" if wants else f"{NO_REPLY_MARKER} ") + body,
                      sender)


def _dispatch(raw_target: str, content: str, sender: Sender, *, wake: Any) -> str:
    """Resolve one target and hand ``content`` to its transport. ``wake`` is how the reply comes
    back: ``"notify"`` (a tracked runner whose completion wakes the sender), ``"none"`` (notify-only:
    nothing waits), or a list collecting the runner commands one fan-out runner joins."""
    from tools.bot_mode_probe import _handle, _profile_name as _self_profile_name
    from tools.bot_relay import _hermes_cli

    root, me, roster_homes, peers = sender.root, sender.me, sender.roster_homes, sender.peers
    roster = list(roster_homes)
    delivery = dict(task_id=sender.task_id, agent=sender.agent, wake=wake)
    # Attribution for the recipient's memory hooks; the text prefix stays the human-facing signature.
    author = {"id": f"bot:{me}", "name": _handle(me, roster), "is_bot": True, **sender.chain}

    # Peer target: '<peer>/<agent>' or a bare registered peer name.
    peer_match = _PEER_TARGET_RE.match(raw_target)
    if peer_match or raw_target.lower() in peers:
        peer_name, peer_profile = peer_match.groups() if peer_match else (raw_target.lower(), None)
        if peer_name not in peers:
            return sender.roster_err(f"No registered peer named '{peer_name}'.")
        dm_target = f"{peer_name}/{peer_profile}" if peer_profile else peer_name
        # A peer dm crosses installs: qualify the id with this host so the peer's own '<me>' stays distinct.
        from agent.turn_author import bot_author_id, local_origin
        peer_author = {**author, "id": bot_author_id(me, local_origin())}
        # Pin the registry-owning profile: `hermes peer` resolves bot_peers via the profile-scoped
        # load_config(), while the roster above reads the machine-root config — the CLI must run
        # in that same profile or a secondary-profile bot sees an empty registry.
        # The delivery runs in a background service context whose PATH lacks the gateway's
        # venv bin dir, so a bare "hermes" resolves to a system install and dies on import
        # under the wrong interpreter (#108628). _hermes_cli pins the entrypoint beside
        # this interpreter; _local_delivery_home matches argv[0] by basename,
        # so the absolute path stays compatible.
        return _start_delivery([_hermes_cli(), "-p", _self_profile_name(root), "peer", "dm", dm_target], content,
                               f"@{peer_profile or peer_name} on peer '{peer_name}'", stdin_file=True,
                               author=peer_author, **delivery)

    # A connection-qualified target ('hermes@mini') names a relay row outright; it is the form the relay itself
    # hands out for a colliding row, and stamps on replies. Resolved locally first, a local bot whose friendly
    # name slugs to 'hermes-mini' captured it. An '@' name no connection answers to still resolves locally.
    if "@" in raw_target.strip().lstrip("@"):
        relay_refusal = _relay_enforce_refusal()
        if relay_refusal is not None:
            return relay_refusal
        # The sender's own relay folder, as below: the default's would relay a named bot's DM as the default.
        relayed = _try_relay_delivery(Path(sender.home), raw_target, content, me, **delivery)
        if relayed is not None:
            return relayed
    # Local teammate — folder id, or a friendly name / Desktop @-slug ('Scribe', 'Dr. Foo').
    resolved = _resolve_local_name(raw_target, roster, root)
    is_local_shape = bool(_LOCAL_TARGET_RE.match(raw_target))
    if resolved is None and not is_local_shape and "@" not in raw_target:
        return sender.roster_err(f"Invalid target: {raw_target!r}.")
    if resolved is None or resolved == me:
        # Unknown locally, or same-name target on ANOTHER connection (this gateway's 'default'
        # messaging the cloud 'default'): every Desktop-connected gateway is reachable via the
        # relay roster, so try that before reporting a resolution failure / self-message.
        relay_refusal = _relay_enforce_refusal()
        if relay_refusal is not None:
            return relay_refusal
        relayed = _try_relay_delivery(Path(sender.home), raw_target, content, me, **delivery)
        if relayed is not None:
            return relayed
        if resolved == me:
            return _err("You can't message yourself. Pick a teammate from the roster.")
        return sender.roster_err(f"No teammate named '{raw_target}' on this install, on a connected "
                                 "machine, or on a registered peer. Pick a name from the roster "
                                 "(roles are listed in your system prompt).")
    if (getattr(sender.agent, "_turn_author", None) or {}).get("id") == f"bot:{resolved}":
        # One reply path (#95074): this turn answers @resolved's delivery, whose runner carries the
        # final response back. A second DM races it and lands in their chat as a separate turn.
        return _err(f"@{_handle(resolved, roster)} messaged you this turn and your final response already goes back "
                    "to them: put your reply in it instead of messaging them. Do not retry.")
    exact_session, route_reason = _resolve_target_bot_session(resolved, sender.origin_session_id)
    if sender.origin_session_id and not exact_session:
        return _err(f"Delivery origin session {sender.origin_session_id!r} is not an eligible Bot Chat "
                    f"for profile '{resolved}' ({route_reason}); refusing title fallback.")
    argv = [_hermes_cli(), "-p", resolved]
    if exact_session:
        argv += ["--resume", exact_session]
    return _start_delivery(argv, content, f"@{_handle(resolved, roster)}",
                           stdin_file=False, profile_home=roster_homes[resolved], author=author, origin_session_id=sender.origin_session_id, route_reason=route_reason, **delivery)


def _resolve_target_bot_session(
    profile: str, origin_session_id: Optional[str]
) -> tuple[Optional[str], str]:
    """Resolve an explicit destination session; title is only a labeled fallback."""
    if not origin_session_id:
        return None, "canonical_title_fallback"
    try:
        from hermes_cli.profiles import get_profile_dir
        from hermes_state import SessionDB

        db = SessionDB(db_path=get_profile_dir(profile) / "state.db")
        try:
            row = db.get_session(str(origin_session_id))
            if not row:
                return None, "origin_session_not_found"
            if str(row.get("title") or "") != "Bot Chat":
                return None, "origin_session_not_bot_chat"
            return db.get_compression_tip(str(origin_session_id)) or str(origin_session_id), "origin_exact"
        finally:
            db.close()
    except Exception:
        logger.debug("exact Bot Chat session resolution failed", exc_info=True)
        return None, "origin_session_unavailable"


def _try_relay_delivery(root: Path, raw_target: str, content: str, me: str, *,
                        task_id: Optional[str], agent: Any, wake: Any = "notify") -> Optional[str]:
    """Cross-connection delivery via the Desktop relay; None when the target doesn't
    resolve against the relay roster. The envelope is queued on disk for the Desktop
    to drain; a background waiter is spawned immediately so the relayed reply wakes
    the sender through the standard completion-notification path."""
    try:
        from tools.bot_mode_probe import _handle, _hermes_root, _roster, local_taken_forms
        from tools.bot_relay import (
            EnvelopeRefusedError, _target_aliases, enqueue_envelope, read_remote_roster, remote_target_forms,
            resolve_remote_target, waiter_command,
        )

        roster = read_remote_roster(root)
        match = resolve_remote_target(raw_target, roster) if roster else None
        if match is None:
            return None
        if match == "ambiguous":
            want = raw_target.strip().lstrip("@").partition("@")[0].lower()
            forms = ", ".join(form for r, form in zip(roster, remote_target_forms(roster, local_taken_forms(root)))
                              if want in _target_aliases(r))
            return _err(f"'{raw_target}' exists on several connected machines — disambiguate with one of: {forms}.")
        relay_refusal = _relay_enforce_refusal()
        if relay_refusal is not None:
            return relay_refusal
        from agent.durable_admission import reserve_observed_effect, observation_gap
        relay_observation = reserve_observed_effect(
            "relay:" + str(match["connection_id"]) + ":" + str(match["handle"]), content,
        )
        if relay_observation is not None:
            # Desktop owns a different ACK seam. This adapter cannot claim it.
            observation_gap(relay_observation, "ack_missing")
        try:
            envelope = enqueue_envelope(root, target=match, message=content, sender_profile=me,
                                       sender_handle=_handle(me, [n for n, _d in _roster(_hermes_root(root))]))
        except EnvelopeRefusedError as exc:
            # Fail fast: target definitively offline — nothing was queued.
            # Structured refusal so the agent can distinguish it from a resolution error ('runtime_offline'
            # per the #93091 reason enum).
            return json.dumps({"error": str(exc), "reason": exc.reason})
        label = f"@{match['handle']} on {match['connection_label'] or match['connection_id']}"
        if wake == "none":
            # The relay delivers the queued envelope on its own; only a reply needs the waiter.
            return json.dumps({"status": "queued", "delivery_id": envelope["id"], "to": label,
                               "expects_reply": False, "detail": _NOTIFY_ONLY_DETAIL})
        raw = _spawn_delivery(waiter_command(root, envelope), label, delivery_id=envelope["id"], task_id=task_id,
                              agent=agent, wake=wake)
        waiter_error = json.loads(raw).get("error")
        if not waiter_error:
            return raw
        # The envelope is already queued and the Desktop drains it on its own, so a waiter that
        # failed to start loses only the reply wake-up. Reporting a hard failure here makes the
        # sender resend and deliver the message twice. Same shape as the live-owner branch of
        # _start_delivery: queued + notification_error.
        return json.dumps({
            "status": "queued", "delivery_id": envelope["id"], "to": label, "notification_error": waiter_error,
            "detail": (f"Message queued for {label}; the relay delivers it on its own, but the reply "
                       "waiter did not start, so the reply will NOT wake you. Do NOT resend."),
        })
    except Exception:
        logger.debug("relay delivery attempt failed", exc_info=True)
        return None


def _dm_dir() -> Path:
    uid_getter = getattr(os, "getuid", None)
    uid = uid_getter() if callable(uid_getter) else None
    path = Path(tempfile.gettempdir()) / (f"{_DM_DIR_NAME}-{uid}" if uid is not None else _DM_DIR_NAME)
    path.mkdir(mode=0o700, exist_ok=True)
    # Shared POSIX temp roots need a per-user directory. Fail closed if an
    # attacker pre-created the expected path or replaced it with a symlink.
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"DM temp path is not a directory: {path}")
    if uid is not None and info.st_uid != uid:
        raise PermissionError(f"DM temp directory is owned by another user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


def cleanup_bot_dm_cache(max_age_hours: float = _DM_STALE_SECONDS / 3600, *, now: float | None = None) -> int:
    """Delete orphaned DM payload files older than *max_age_hours*; returns count.
    Same contract as the other ``cleanup_*_cache`` helpers (hourly gateway housekeeping);
    legacy temp-root locations from versions predating the dedicated directory are swept too."""
    temp_root = Path(tempfile.gettempdir())
    locations = [(temp_root, "hermes-dm-*.txt"), (temp_root, "hermes-relay-dm-*.txt")]
    with contextlib.suppress(OSError):
        dm_dir = _dm_dir()
        locations.append((dm_dir, "*.txt"))
        # Live-delivery intents (``<dm file>.live.json``, message plaintext included) outlive
        # their runner on purpose — a retry replays the same delivery id from them — so the
        # orphans of runners that never settled are swept here too.
        locations.append((dm_dir, "*.live.json"))
        locations.append((dm_dir, "fanout-*.json"))
    from utils import unlink_files_older_than

    return sum(unlink_files_older_than(d, pattern, max_age_hours * 3600, now=now) for d, pattern in locations)


def _unlink_dm_file(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _write_dm_file(content: str) -> str:
    """The message rides a temp file — never inline shell text."""
    cleanup_bot_dm_cache()
    fd, path = tempfile.mkstemp(prefix="dm-", suffix=".txt", dir=_dm_dir(), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        # If fdopen itself failed the raw descriptor is still ours; closing twice is harmless.
        with contextlib.suppress(OSError):
            os.close(fd)
        _unlink_dm_file(path)
        raise
    return path


def _delivery_ledger_path(idempotency_key: str) -> Path:
    return _dm_dir() / ("delivery-" + idempotency_key + ".json")


def _delivery_envelope_path(dm_file: str) -> Path:
    return Path(dm_file + ".delivery.json")


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_canonical(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        _unlink_dm_file(temporary)
        raise


from tools.bot_mode_dm_ledger import _delivery_ledger_lock


from tools.bot_mode_dm_ledger import _read_delivery_ledger


from tools.bot_mode_dm_ledger import _write_delivery_receipt


def _update_delivery_receipt(dm_file: str, state: str, *, ack: Optional[dict[str, Any]] = None,
                             ack_row_validated: bool = False) -> None:
    try:
        record = json.loads(Path(dm_file + ".receipt.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        from agent.durable_admission import observation_enabled, observation_gap
        if observation_enabled():
            observation_gap(None, "ledger_failed")
            return
        record = {}
    if not record.get("observation") and not record.get("enforced_effect"):
        return _update_delivery_receipt_inner(dm_file, state, ack=ack, ack_row_validated=ack_row_validated)
    from agent.durable_admission import observation_gap
    try:
        key = record["idempotency_key"]
        with _delivery_ledger_lock(key):
            canonical = _read_delivery_ledger(_delivery_ledger_path(key), key)
            if canonical is None or canonical["delivery_id"] != record["delivery_id"]:
                raise ValueError("delivery ledger mismatch")
            if canonical.get("adapter_ack") is not None:
                return
            return _update_delivery_receipt_inner(dm_file, state, ack=ack, ack_row_validated=ack_row_validated)
    except Exception:
        logging.getLogger(__name__).debug('_update_delivery_receipt: optional operation failed', exc_info=True)
        observation_gap(record.get("observation"), "ledger_failed")


def _update_delivery_receipt_inner(dm_file: str, state: str, *, ack=None, ack_row_validated=False):
    path = Path(dm_file + ".receipt.json")
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if (record.get("observation") or record.get("enforced_effect")) and record.get("adapter_ack") is not None:
            return  # ACK/release replay: never fence, renew, resend or release.
        record["state"] = state
        record["updated_at"] = int(time.time())
        if ack is not None:
            record["adapter_ack"] = dict(ack)
            record["adapter_receipt_sha256"] = _canonical_sha256(ack)
        _atomic_json(path, record)
        idempotency_key = str(record.get("idempotency_key") or "")
        if idempotency_key:
            ledger = _delivery_ledger_path(idempotency_key)
            _atomic_json(ledger, record)
        if ack is not None and state == "delivered":
            if record.get("observation"):
                _settle_observed_delivery(record, path, ack, ack_row_validated=ack_row_validated)
            elif record.get("enforced_effect"):
                _settle_enforced_delivery(record, path, ack, ack_row_validated=ack_row_validated)
            else:
                _emit_delivery_shadow_receipt(record, state, ack_bytes=_canonical(ack))
        elif record.get("observation"):
            from agent.durable_admission import observation_gap
            observation_gap(record["observation"], "ack_missing" if state in {"delivered", "unknown"} else "delivery_failed")
    except (OSError, ValueError, TypeError):
        if isinstance(locals().get("record"), dict) and record.get("observation"):
            from agent.durable_admission import observation_gap
            observation_gap(record["observation"], "ledger_failed")
        else:
            logger.debug("delivery receipt update failed", exc_info=True)


def _settle_enforced_delivery(record: dict, path: Path, ack: dict, *, ack_row_validated: bool):
    """Only the independently persisted destination ACK releases our effect."""
    if not ack_row_validated:
        record["release_state"] = "ack_not_validated"
    else:
        from agent.durable_admission import release_observed_effect
        _emit_delivery_shadow_receipt(record, "delivered", ack_bytes=_canonical(ack))
        record["release_state"] = "requested"
        _atomic_json(path, record)
        _atomic_json(_delivery_ledger_path(record["idempotency_key"]), record)
        try:
            record["release_receipt"] = release_observed_effect(record["turn_identity"], record["release_event_id"])
            record["release_state"] = "acknowledged"
        except Exception:
            logging.getLogger(__name__).debug('_settle_enforced_delivery: optional operation failed', exc_info=True)
            record["release_state"] = "unknown"
    _atomic_json(path, record)
    _atomic_json(_delivery_ledger_path(record["idempotency_key"]), record)


def _settle_observed_delivery(record: dict, path: Path, ack: dict, *, ack_row_validated: bool):
    from agent.durable_admission import ObservationWriter, observation_gap, release_observed_effect

    observation = record["observation"]
    if not ack_row_validated:
        observation_gap(observation, "ack_missing")
        return
    try:
        spool = _emit_delivery_shadow_receipt(record, "delivered", ack_bytes=_canonical(ack))
        if spool is None:
            raise ValueError("spool disabled")
    except Exception:
        logging.getLogger(__name__).debug('_settle_observed_delivery: optional operation failed', exc_info=True)
        observation_gap(observation, "spool_failed")
        return
    try:
        record["release_state"] = "requested"
        _atomic_json(path, record)
        ledger = _delivery_ledger_path(record["idempotency_key"])
        _atomic_json(ledger, record)
        ledger_hash = hashlib.sha256(ledger.read_bytes()).hexdigest()
    except Exception:
        logging.getLogger(__name__).debug('_settle_observed_delivery: optional operation failed', exc_info=True)
        observation_gap(observation, "ledger_failed")
        return
    try:
        release = release_observed_effect(record["turn_identity"], record["release_event_id"])
    except Exception:
        logging.getLogger(__name__).debug('_settle_observed_delivery: optional operation failed', exc_info=True)
        observation_gap(observation, "release_ambiguous")
        return
    try:
        spool_document = json.loads(spool.read_text(encoding="utf-8-sig"))
        record["release_state"] = "acknowledged"
        record["release_receipt"] = release
        _atomic_json(path, record)
        _atomic_json(ledger, record)
        ledger_hash = hashlib.sha256(ledger.read_bytes()).hexdigest()
        identity = record["turn_identity"]
        writer = ObservationWriter(Path(observation["root"]), code_sha=observation["code_sha"], boot_id=observation["effect"]["boot_id"])
        writer.transition(observation["effect"]["effect_id"], "settled", settlement={
            "work_id": identity["work_id"], "attempt_id": identity["attempt_id"], "generation": identity["generation"],
            "delivery_id": record["delivery_id"], "delivery_event_id": spool_document["event_id"],
            "ack_sha256": _canonical_sha256(ack), "ledger_sha256": ledger_hash,
            "spool_sha256": hashlib.sha256(spool.read_bytes()).hexdigest(),
            "release_event_id": record["release_event_id"], "release_source_event_id": release["source_event_id"],
            "release_receipt_sha256": _canonical_sha256(release),
        })
    except Exception:
        logging.getLogger(__name__).debug('_settle_observed_delivery: optional operation failed', exc_info=True)
        observation_gap(observation, "storage_failed")


def _emit_delivery_shadow_receipt(
    record: dict[str, Any], state: str, *, ack_bytes: Optional[str] = None
) -> Optional[Path]:
    """Observe a terminal DM effect only with an upstream-validated fence."""
    from hermes_cli.operational_settings import shadow_producer_enabled

    if not shadow_producer_enabled():
        return
    if state != "delivered" or not ack_bytes:
        return
    try:
        from gateway.turn_context import write_kernel_shadow_receipt
        identity = record["turn_identity"]
        fence = record["fence"] if record.get("observation") else _revalidate_delivery_identity(record)
        work_id = identity["work_id"]
        authority_version = identity["authority_version"]
        tenant = identity["tenant"]
        profile = identity["profile"]
        source = identity["source"]
        attempt_id = identity["attempt_id"]
        lease_epoch = identity["generation"]
        delivery_id = str(record["delivery_id"])
        origin = str(record["origin_session_id"])
        source_event_id = str(identity["source_event_id"])
        adapter_hash = hashlib.sha256(ack_bytes.encode("utf-8")).hexdigest()
        event_id = "agent-delivery-" + hashlib.sha256(
            (work_id + "\0" + delivery_id + "\0" + state).encode("utf-8")
        ).hexdigest()
        return write_kernel_shadow_receipt({
            "schema": "hermes.kernel-shadow-event/v1",
            "event_id": event_id,
            "work_id": work_id,
            "authority_version": authority_version,
            "tenant": tenant,
            "profile": profile,
            "source": source,
            "origin_session_id": origin,
            "source_event_id": source_event_id,
            "attempt_id": attempt_id,
            "lease_epoch": lease_epoch,
            "action": "delivery",
            "delivery_id": delivery_id,
            "question_id": "",
            "outcome": state,
            "adapter_receipt_sha256": adapter_hash,
            "observed_at": int(time.time()),
            "fence_valid": bool(fence["fence_valid"]),
            "material": False,
            "waiting_for_human": False,
            # Terminal DELIVERY, never Work completion. The separate release
            # projection remains action=terminal, outcome=unknown, terminal=false.
            "terminal": True,
        })
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        if record.get("observation"):
            raise
        logger.debug("kernel shadow delivery receipt skipped", exc_info=True)


def _revalidate_delivery_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    from agent.durable_admission import _run_turn_identity_revalidation

    identity = record.get("turn_identity")
    if not isinstance(identity, Mapping):
        raise RuntimeError("delivery_turn_identity_missing")
    return _run_turn_identity_revalidation(identity)


def _validated_delivery_ack(
    payload: Any, record: Mapping[str, Any], *, expected_adapter: Optional[str] = None
) -> dict[str, Any]:
    ack = payload.get("delivery_ack") if isinstance(payload, Mapping) else None
    identity = record.get("turn_identity")
    if not isinstance(ack, Mapping) or not isinstance(identity, Mapping):
        raise ValueError("delivery_ack_missing")
    required = {
        "schema", "delivery_id", "request_key", "turn_identity_sha256",
        "target_session_id", "target_message_id", "adapter", "state",
        "accepted_at", "persisted_at",
    }
    if set(ack) != required or ack.get("schema") != DELIVERY_ACK_SCHEMA:
        raise ValueError("delivery_ack_fields_invalid")
    expected = {
        "delivery_id": record.get("delivery_id"),
        "request_key": identity.get("request_key"),
        "turn_identity_sha256": _canonical_sha256(identity),
        "accepted_at": record.get("accepted_at"),
        "state": "persisted",
    }
    if any(ack.get(key) != value for key, value in expected.items()):
        raise ValueError("delivery_ack_rebind")
    if ack.get("adapter") not in {"cli", "api_server"}:
        raise ValueError("delivery_ack_adapter_invalid")
    if expected_adapter is not None and ack.get("adapter") != expected_adapter:
        raise ValueError("delivery_ack_adapter_rebind")
    if not isinstance(ack.get("target_session_id"), str) or not ack["target_session_id"]:
        raise ValueError("delivery_ack_session_invalid")
    if isinstance(ack.get("target_message_id"), bool) or not isinstance(ack.get("target_message_id"), int) or ack["target_message_id"] < 1:
        raise ValueError("delivery_ack_message_invalid")
    if isinstance(ack.get("persisted_at"), bool) or not isinstance(ack.get("persisted_at"), int) or ack["persisted_at"] < 1:
        raise ValueError("delivery_ack_timestamp_invalid")
    response_session = payload.get("session_id") if isinstance(payload, Mapping) else None
    if response_session != ack["target_session_id"]:
        raise ValueError("delivery_ack_session_rebind")
    return dict(ack)


def _delivery_envelope_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    identity = record["turn_identity"]
    return {
        "schema": DELIVERY_ENVELOPE_SCHEMA,
        "delivery_id": record["delivery_id"],
        "request_key": identity["request_key"],
        "turn_identity_sha256": _canonical_sha256(identity),
        "accepted_at": int(record["accepted_at"]),
    }


def _validate_local_ack_row(db: Any, ack: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    around = db.get_messages_around(
        ack["target_session_id"], int(ack["target_message_id"]), window=0
    )
    rows = around.get("window") if isinstance(around, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("delivery_ack_target_message_missing")
    row = rows[0]
    metadata = row.get("display_metadata") if isinstance(row, Mapping) else None
    if (not isinstance(metadata, Mapping)
            or metadata.get("hermes_delivery") != _delivery_envelope_from_record(record)
            or row.get("role") != "user"):
        raise ValueError("delivery_ack_target_message_rebind")


def _validate_local_ack_against_destination(
    ack: Mapping[str, Any], record: Mapping[str, Any], argv: list[str]
) -> None:
    try:
        profile = argv[argv.index("-p") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("delivery_ack_target_profile_missing") from exc
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB

    db = SessionDB(db_path=get_profile_dir(profile) / "state.db")
    try:
        _validate_local_ack_row(db, ack, record)
    finally:
        db.close()


def _release_unspawned_delivery(dm_file: Optional[str]) -> None:
    """Remove a pre-spawn claim so the same idempotency key may retry."""
    if not dm_file:
        return
    path = Path(dm_file + ".receipt.json")
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        key = str(record.get("idempotency_key") or "")
        if key:
            _unlink_dm_file(str(_delivery_ledger_path(key)))
    except (OSError, ValueError, TypeError):
        pass
    _unlink_dm_file(str(path))
    _unlink_dm_file(str(_delivery_envelope_path(dm_file)))


def _delivery_lock(argv: list[str], *, stdin_file: bool):
    """Per-profile turn lock for a LOCAL teammate delivery: local and relay deliveries
    into one profile both run a Bot Chat turn here, so the turn window is serialized on
    ``tools.bot_relay``'s cross-process lock. Peer transports (stdin mode) are locked
    on the remote gateway by its own deliver path.

    See #93091.
    """
    # Match the CLI element by basename: argv[0] may be an absolute venv path
    # (service contexts lack PATH) and carries .exe on Windows; split on both separators.
    # Split on both separators so the shape matches regardless of which platform built the argv. See #93590.
    cli = (argv[0] if argv else "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if stdin_file or len(argv) < 3 or cli not in ("hermes", "hermes.exe") or argv[1] != "-p":
        return contextlib.nullcontext()
    from tools.bot_mode_probe import _hermes_root
    from tools.bot_relay import acquire_turn_lock

    return acquire_turn_lock(_hermes_root(Path(_default_home())), argv[2])


def _live_intent_file(dm_file: str | os.PathLike) -> str:
    """The pinned live-delivery intent beside a DM file (the cache sweep globs ``*.live.json``)."""
    return f"{os.fspath(dm_file)}.live.json"




def _run_local_turn(argv: list[str], dm_file: str, *, env: Optional[dict[str, str]] = None) -> int:
    """One Bot Chat turn via ``--query-file`` (plus one policy-gated retry); re-emits
    the transport's streams and returns its exit code. Transient failures re-run the
    same session; a context_overflow re-run lets the retried turn's pre-API compaction
    compact the transcript first (no fresh session is ever minted). Auth/quota/config never retry."""

    def _turn(turn_env=env):
        return subprocess.run([*argv, "--query-file", dm_file], check=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=turn_env, timeout=_LIVE_WAIT_MAX_SECONDS)

    proc = _turn()
    if proc.returncode != 0:
        from tools.bot_failure_reasons import RETRY_NONE, classify_agent_error, retry_action, turn_failure_text
        from tools.bot_relay import retry_turn_env

        # The re-run replays the same session and payload; the failed attempt already persisted the
        # user row, so the retried process is told to resume it (RESUME_UNANSWERED_TURN_ENV).
        if retry_action(classify_agent_error(turn_failure_text(proc.stdout, proc.stderr))) != RETRY_NONE:
            proc = _turn(retry_turn_env(env))
    stderr_text = proc.stderr or ""
    reason = next((line.removeprefix("hermes-refusal-reason: ").strip()
                   for line in stderr_text.splitlines()
                   if line.startswith("hermes-refusal-reason: ")), None)
    # A code wins over prose, including unknown codes from newer CLIs.
    # Only older CLIs without a marker need the historical wording fallback.
    refused_not_owned = (reason == "SESSION_NOT_OWNED" if reason is not None
                         else "already has a live owner" in stderr_text)
    if proc.returncode != 0 and refused_not_owned:
        # The target's Bot Chat is held live by another surface (Desktop); the turn
        # never ran — tell the sender plainly instead of leaking a raw lease error.
        # See #100523.
        who = argv[argv.index("-p") + 1] if "-p" in argv[:-1] else "the teammate"
        print(json.dumps({
            "error": f"Delivery failed: @{who}'s Bot Chat is open on another "
                     "surface right now, so your message was NOT delivered. Try again later.",
            "reason": "target_busy",
        }))
        return 1
    # Re-emit the transport's streams: stdout is the reply text the
    # completion notification carries back to the sending agent. A successful bare
    # silence marker is a delivery decision (same rule as the gateway and the live
    # Bot Chat completion): the turn stays in the target's transcript, the sender
    # never sees the marker as prose.
    from gateway.response_filters import is_intentional_silence_response
    reply = proc.stdout or ""
    if proc.returncode == 0 and is_intentional_silence_response(reply):
        reply = ""
    for stream, text in ((sys.stdout, reply), (sys.stderr, proc.stderr)):
        if text:
            stream.write(text)
            stream.flush()
    return proc.returncode


def _dm_delivery_id(dm_file: str | os.PathLike) -> str:
    """One delivery id per DM file: the dispatch ack, the live-owner intent and every retry
    of the runner derive it the same way, so the sender can correlate all of them."""
    return hashlib.sha256(str(Path(dm_file).resolve()).encode()).hexdigest()


class LiveTargetUnavailableError(RuntimeError):
    """The canonical Bot Chat owner could not be reached with anything admitted — before the
    intent was pinned, or on a dial that died before the deliver RPC went out: nothing was
    sent, so this is a definite no-send the sender may retry, not the ambiguous
    post-admission state."""

    reason = "runtime_unavailable"
    resend = "once the target is up"


class LiveTargetHeldError(LiveTargetUnavailableError):
    """The owner refused because another session writer holds the Bot Chat, and settled this
    delivery id as never sent (``bot_relay.settle``): every later attempt under the id gets that
    disposition, so this is a definite no-send too. A refusal without that settlement stays the
    ambiguous outcome."""

    resend = "after that session closes"

    @property
    def reason(self) -> str:
        from tools.bot_failure_reasons import HELD_NOT_SENT
        return HELD_NOT_SENT


def _unavailable_payload(dm_file: str, exc: LiveTargetUnavailableError) -> str:
    return json.dumps({"reason": exc.reason, "delivery_id": _dm_delivery_id(dm_file),
                       "error": f"{exc} — the message was NOT sent; it is safe to resend {exc.resend}."})


def _settled_no_send(dm_file: str, exc: LiveTargetUnavailableError) -> str:
    """The answer to a definite no-send, for both the tool and its runner: first the payload and
    any pinned intent go, so nothing local can rebuild or replay the delivery. A file that is still
    there is not settlement: the answer is then the unknown outcome, never "safe to resend"."""
    left = []
    for path in (_live_intent_file(dm_file), dm_file):
        _unlink_dm_file(path)
        if os.path.lexists(path):
            left.append(path)
    if left:
        return _live_outcome_unknown(dm_file, f"could not remove {left[0]}")
    return _unavailable_payload(dm_file, exc)


def _held_disposition(record: dict | None) -> bool:
    """The owner's terminal not-sent receipt for a delivery refused while another writer held the
    chat (``gateway/session_bot.py:_not_sent_record``)."""
    from tools.bot_failure_reasons import HELD_NOT_SENT
    return bool(record) and record.get("refused") is True and record.get("reason") == HELD_NOT_SENT


def _admit_live_dm(profile_home: Path | None, dm_file: str, author: Optional[dict] = None) -> dict | None:
    """Pin intent before admission; retries may inspect, never change transport."""
    from tools.bot_live_delivery import (PreDialError, deliver_to_live_owner, find_canonical_live_owner,
                                         read_delivery_result)
    from utils import fsync_directory

    intent: dict[str, Any]
    intent_path = Path(_live_intent_file(dm_file))
    pinned_now = False
    if intent_path.exists():
        intent = json.loads(intent_path.read_text(encoding="utf-8-sig"))
    else:
        assert profile_home is not None
        try:
            owner = find_canonical_live_owner(profile_home)
        except Exception as exc:
            # Discovery runs before the intent exists and never admits: definite, not ambiguous.
            raise LiveTargetUnavailableError(f"canonical Bot Chat target is unavailable ({exc})") from exc
        if owner is None:
            raise LiveTargetUnavailableError("canonical Bot Chat target is unavailable")
        intent = dict(owner=owner, message=Path(dm_file).read_text(encoding="utf-8-sig"),
                      delivery_id=_dm_delivery_id(dm_file),
                      **({"author": author} if author else {}))
        try:
            fd = os.open(intent_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            intent = json.loads(intent_path.read_text(encoding="utf-8-sig"))
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(intent, stream)
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(intent_path.parent)
            pinned_now = True
    home = intent["owner"]["profile_home"]
    record = read_delivery_result(home, intent["delivery_id"])
    if record is None or intent["owner"].get("canonical"):
        from hermes_cli.gateway_client import GatewayRPCError
        from tools.bot_failure_reasons import HELD_NOT_SENT
        from tools.bot_live_delivery import settle_held_delivery

        identity = dict(delivery_id=intent["delivery_id"], author=intent.get("author"))
        try:
            record = deliver_to_live_owner(home, intent["owner"], intent["message"], **identity)
        except GatewayRPCError as exc:
            if str(exc) != HELD_NOT_SENT or exc.data.get("admitted") is not False:
                raise
            # Nothing was admitted while that refusal held the owner's lock, but a same-id attempt
            # may have admitted since: only the owner's settle decides, under that same lock.
            record = settle_held_delivery(home, intent["owner"], intent["message"], **identity)
        except PreDialError as exc:
            if not pinned_now:
                raise  # an earlier attempt under this intent may have admitted: outcome unknown
            # This call pinned the intent and its first dial died before the RPC went out: nothing
            # was admitted anywhere, so it is a definite no-send the sender may retry (#139303).
            _unlink_dm_file(_live_intent_file(dm_file))
            raise LiveTargetUnavailableError(f"live authority dial failed before admission ({exc})") from exc
    if _held_disposition(record):
        # The callers remove the intent and payload (``_settled_no_send``).
        raise LiveTargetHeldError("Bot Chat is held by another session writer")
    return record


def _wait_live_dm(home: str, delivery_id: str, *, dm_file: str | os.PathLike | None = None,
                  on_result=None) -> int:
    from tools.bot_failure_reasons import RUNTIME_OFFLINE
    from tools.bot_live_delivery import await_delivery, cancel_queued_delivery, owner_holds_delivery

    deadline = time.monotonic() + _LIVE_WAIT_MAX_SECONDS
    missed, unreachable = 0, False
    while True:
        record = await_delivery(home, delivery_id, max(0.0, min(_LIVE_WAIT_SECONDS, deadline - time.monotonic())))
        if record is None or record["status"] not in ("queued", "claimed") or time.monotonic() >= deadline:
            break
        try:
            held = owner_holds_delivery(home, record)
        except Exception:
            logger.debug("Could not check live DM owner", exc_info=True)
            held = None
        unreachable = held is None
        if held:
            missed = 0
            continue
        if held is False and record["status"] == "queued":
            # Under the same mailbox lock as claim, either cancellation wins or
            # the claim wins and we must keep waiting for its outcome.
            record = cancel_queued_delivery(
                home, delivery_id, error="Live Bot Chat owner is no longer available",
                reason=RUNTIME_OFFLINE)
            if record is None or record["status"] != "claimed":
                break
            missed = 0
            continue
        # A vanished consumer may leave a permanent claim, and an owner we cannot look up
        # may be gone: allow one more window for an in-flight turn, then release the runner.
        missed += 1
        if missed > 1:
            break
    status = record["status"] if record else "ambiguous"
    if on_result is not None:
        on_result(status)
    payload = {key: record[key] for key in ("reply", "error", "reason") if record and record.get(key)}
    payload.update(status=status, delivery_id=delivery_id)
    if status in ("queued", "claimed", "ambiguous"):
        payload["detail"] = "Delivery remains pending or its outcome is unknown. Do not resend; receipt is retained."
        state = _live_target_state(dm_file) or ("unreachable" if unreachable else None)
        if state:
            # A bound that lapses on a teammate parked on its own user is that, never a silent timeout.
            payload["target_state"] = state
    elif status == "settled" and dm_file is not None:
        # The intent carries the message plaintext so a retry can replay the SAME delivery id;
        # once the owner settled it nothing retries, so it goes along with the dm file (same
        # plaintext) — the live branch returns before _run_delivery's own unlink.
        _unlink_dm_file(_live_intent_file(dm_file))
        _unlink_dm_file(str(dm_file))
    from tools.bot_mode_dm_outcome import outcome_line

    who = f"@{Path(home).name}" if Path(home).parent.name == "profiles" else "Your teammate"
    print(json.dumps({"outcome": outcome_line(who, payload), **payload}))
    return 0 if status in ("settled", "queued", "claimed") else 1


def _live_target_state(dm_file: str | os.PathLike | None) -> Optional[str]:
    """The target Bot Chat's live state for a wait ending pending, from a same-id replay of the
    pinned intent (the owner answers it with the existing receipt). None when nothing answers:
    the state is a hint, and its absence must never change the delivery outcome."""
    if dm_file is None or not os.path.exists(_live_intent_file(dm_file)):
        return None
    try:
        return (_admit_live_dm(None, os.fspath(dm_file)) or {}).get("target_state")
    except Exception:
        logger.debug("Could not read the live DM target's state", exc_info=True)
        return None


def _local_delivery_home(argv: list[str]) -> Path | None:
    cli = (argv[0] if argv else "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if len(argv) < 3 or cli not in ("hermes", "hermes.exe") or argv[1] != "-p":
        return None
    from tools.bot_mode_probe import _hermes_root, _roster

    return dict(_roster(_hermes_root(Path(_default_home())))).get(argv[2])


def _live_outcome_unknown(dm_file: str, cause: object) -> str:
    """The runner's stdout when a live admission may have happened but its outcome is unknown."""
    return json.dumps({"status": "ambiguous", "delivery_id": _dm_delivery_id(dm_file),
                       "error": f"Live admission outcome unknown: {cause}. Do not resend.",
                       "evidence_file": dm_file})


def _runner_argv(args: list[str]) -> tuple[Optional[str], str, str, list[str]] | None:
    """Split ``--run-delivery [--author <json>] <mode> <dm_file> <argv…>`` into
    ``(author_json, mode, dm_file, argv)``; None when malformed. Stdlib only: the boot-failure
    report runs it when no Hermes import is available."""
    if args[:1] != ["--run-delivery"]:
        return None
    rest, author = args[1:], None
    if rest[:1] == ["--author"]:
        if len(rest) < 2:
            return None
        author, rest = rest[1], rest[2:]
    if len(rest) < 2 or rest[0] not in ("stdin", "query-file"):
        return None
    return author, rest[0], rest[1], rest[2:]


def _run_live_delivery(argv, dm_file, profile_home, author):
    """Admit once to the canonical owner; only its settled receipt proves delivery."""
    home = profile_home or _local_delivery_home(argv)
    if home is None and not os.path.exists(_live_intent_file(dm_file)):
        print(json.dumps({"reason": "runtime_unavailable", "error": "No canonical Bot Chat authority; payload retained."}))
        return 1, None
    try:
        live_record = _admit_live_dm(home, dm_file, author)
    except LiveTargetHeldError as exc:
        print(_settled_no_send(dm_file, exc))
        return 1, None
    except LiveTargetUnavailableError as exc:
        print(_unavailable_payload(dm_file, exc))
        return 1, None
    except Exception as exc:
        logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
        print(_live_outcome_unknown(dm_file, exc))
        return 1, None
    if live_record is None:
        print(json.dumps({"reason": "runtime_unavailable", "error": "No canonical Bot Chat authority; payload retained."}))
        return 1, None
    live_state = "unknown"
    def capture_live_status(status):
        nonlocal live_state
        live_state = "delivered" if status == "settled" else (
            "failed" if status in ("failed", "cancelled") else "unknown")
    returncode = _wait_live_dm(live_record["profile_home"], live_record["delivery_id"],
                               dm_file=dm_file, on_result=capture_live_status)
    return returncode, live_state


def _run_delivery_transport(transport_argv, dm_file, stdin_file, env):
    """Run the declared transport, retrying only an explicit retryable failure."""
    if stdin_file:
        # Keep the file open until the transport exits; cleanup occurs
        # after subprocess.run returns, not merely after stdin reaches EOF.
        with open(dm_file, "r", encoding="utf-8-sig") as stream:
            proc = subprocess.run(
                transport_argv, input=stream.read(), check=False, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env, timeout=_LIVE_WAIT_MAX_SECONDS,
            )
    else:
        proc = subprocess.run(
            [*transport_argv, "--query-file", dm_file],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace", env=env, timeout=_LIVE_WAIT_MAX_SECONDS,
        )
    if proc.returncode != 0 and not stdin_file:
        from tools.bot_failure_reasons import (
            RETRY_NONE,
            classify_agent_error,
            retry_action,
            turn_failure_text,
        )
        from tools.bot_relay import retry_turn_env

        detail = turn_failure_text(proc.stdout, proc.stderr)
        if retry_action(classify_agent_error(detail)) != RETRY_NONE:
            proc = subprocess.run(
                [*transport_argv, "--query-file", dm_file],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                env=retry_turn_env(env), timeout=_LIVE_WAIT_MAX_SECONDS,
            )

    return proc


def _consume_delivery_transport(proc, record, argv, *, structured: bool, stdin_file: bool):
    """Validate transport ACK evidence and forward its response streams."""
    returncode, ack, ack_row_validated = proc.returncode, None, False
    # Re-emit the transport's streams: stdout is the reply text the
    # completion notification carries back to the sending agent.
    if structured and proc.returncode == 0:
        try:
            payload = json.loads(proc.stdout or "")
            ack = _validated_delivery_ack(
                payload, record,
                expected_adapter="api_server" if stdin_file else "cli",
            )
            if not stdin_file:
                _validate_local_ack_against_destination(ack, record, argv)
                ack_row_validated = True
            reply = str(payload.get("reply") or "")
        except Exception as exc:
            if record.get("observation"):
                from agent.durable_admission import observation_gap
                observation_gap(record["observation"], "ack_missing")
                ack = None
                reply = proc.stdout or ""
            else:
                if not isinstance(exc, (TypeError, ValueError)):
                    raise
                returncode = 1
                reply = ""
        if reply:
            sys.stdout.write(reply)
            sys.stdout.flush()
    elif proc.stdout:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    return returncode, ack, ack_row_validated


def _run_delivery(argv: list[str], dm_file: str, *, stdin_file: bool,
                  profile_home: Path | None = None, author: Optional[dict] = None,
                  lock_held: bool = False, strict_authority: bool = False) -> int:
    """Route to the live owner before attempting a CLI transport. Live deliveries
    retain their intent/payload and immutable receipt; only CLI/peer payloads are
    removed after consumption. The CLI turn window holds the profile lock, so two
    deliveries into one profile queue; a bounded wait ends in a 'target_busy' refusal.
    ``author`` rides to the child as HERMES_TURN_AUTHOR; ``hermes peer dm`` forwards it in the request body.

    The optional peer stdin transport remains an explicit remote route, never a
    fallback after local admission. Background processes wait for delivery only.
    """
    from tools.bot_delivery_runtime_env import _delivery_runtime_env
    returncode = 1
    keep_live_intent = False
    live_state = None
    record = {}
    structured = False
    ack: Optional[dict[str, Any]] = None
    ack_row_validated = False
    try:
        receipt_path = Path(dm_file + ".receipt.json")
        try:
            record = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            record = {}
        structured = isinstance(record.get("turn_identity"), dict)
        if record.get("observation") or record.get("enforced_effect"):
            key = record["idempotency_key"]
            with _delivery_ledger_lock(key):
                canonical = _read_delivery_ledger(_delivery_ledger_path(key), key)
                if canonical is not None and canonical.get("adapter_ack") is not None:
                    if canonical["delivery_id"] != record["delivery_id"]:
                        raise ValueError("delivery ledger mismatch")
                    # The effect already has an independent ACK. No fence,
                    # transport or release is legal on a child replay either.
                    ack = canonical["adapter_ack"]
                    returncode = 0
                    return returncode
        from tools.bot_relay import delivery_env
        author = author or record.get("turn_author")
        if profile_home is None and record.get("profile_home"):
            profile_home = Path(record["profile_home"])
        env = delivery_env(author, profile_home if not stdin_file else None)
        # delivery_env starts from ambient env; restore our release path afterwards.
        env["PYTHONPATH"] = _delivery_runtime_env()["PYTHONPATH"]
        if not structured and not record.get("observation") and not record.get("enforced_effect") and not stdin_file:
            keep_live_intent = True
            returncode, live_state = _run_live_delivery(argv, dm_file, profile_home, author)
            return returncode
        if not structured and stdin_file and not record.get("observation") and not record.get("enforced_effect"):
            with _delivery_lock(argv, stdin_file=True):
                with open(dm_file, "r", encoding="utf-8-sig") as stream:
                    returncode = subprocess.run(argv, input=stream.read().encode("utf-8"), check=False, env=env, timeout=_LIVE_WAIT_MAX_SECONDS).returncode
                    return returncode
        transport_argv = list(argv)
        if not stdin_file and "chat" not in transport_argv:
            transport_argv += ["chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q"]
        if structured:
            try:
                fresh_fence = _revalidate_delivery_identity(record)
                if (
                    fresh_fence.get("fence_valid") is not True
                    or fresh_fence.get("identity_sha256") != _canonical_sha256(record["turn_identity"])
                ):
                    raise ValueError("delivery fence invalid")
            except Exception:
                logging.getLogger(__name__).debug('_run_delivery: optional operation failed', exc_info=True)
                if record.get("observation") and not strict_authority:
                    from agent.durable_admission import observation_gap
                    observation_gap(record["observation"], "admission_failed")
                    structured = False
                else:
                    return 1
        if structured:
            if stdin_file:
                transport_argv += ["--delivery-envelope-file", str(_delivery_envelope_path(dm_file))]
                transport_argv.append("--json")
            else:
                message = Path(dm_file).read_text(encoding="utf-8-sig")
                Path(dm_file).write_text(
                    _encode_delivery_query(
                        json.loads(_delivery_envelope_path(dm_file).read_text(encoding="utf-8-sig")),
                        message,
                    ),
                    encoding="utf-8",
                )
                os.chmod(dm_file, 0o600)
        with (contextlib.nullcontext() if lock_held else _delivery_lock(argv, stdin_file=stdin_file)):
            proc = _run_delivery_transport(transport_argv, dm_file, stdin_file, env)
            returncode = proc.returncode
            returncode, ack, ack_row_validated = _consume_delivery_transport(
                proc, record, argv, structured=structured, stdin_file=stdin_file)
            return returncode
    except subprocess.TimeoutExpired:
        # The child may already have admitted the turn. Keep evidence/payload and
        # classify ambiguity without issuing another model-facing turn.
        live_state = "unknown"
        keep_live_intent = True
        print(json.dumps({"reason": "delivery_timeout", "error": "Delivery outcome unknown; no retry was started."}))
        return 1
    finally:
        state = "delivered" if returncode == 0 and (ack is not None or not structured) else "failed"
        if live_state is not None:
            state = live_state
        if record.get("observation") and returncode == 0 and structured and ack is None:
            state = "unknown"
        if record.get("observation") or record.get("enforced_effect"):
            _update_delivery_receipt(dm_file, state, ack=ack, ack_row_validated=ack_row_validated)
        else:
            _update_delivery_receipt(dm_file, state, ack=ack)
        if not keep_live_intent:
            _unlink_dm_file(dm_file)
            _unlink_dm_file(str(_delivery_envelope_path(dm_file)))


def _delivery_command(argv: list[str], dm_file: str, *, stdin_file: bool,
                      profile_home: Path | None = None, author: Optional[dict] = None) -> str:
    """Build an argv-safe command for the cleanup-owning background runner:
    ``--run-delivery [--author <json>] <mode> <dm_file> [--profile-home <path>] <argv...>``."""
    runner_argv = [sys.executable, str(Path(__file__).resolve()), "--run-delivery",
                   "stdin" if stdin_file else "query-file", dm_file]
    if profile_home is not None:
        runner_argv.extend(["--profile-home", str(Path(profile_home).resolve())])
    runner_argv.extend(argv)
    if sys.platform == "win32":
        # The tracked local backend uses Git Bash on native Windows: forward slashes keep drive
        # paths executable there; backslash paths are parsed as command names (exit 127).
        runner_argv = [part.replace("\\", "/") for part in runner_argv]
    if author:
        # Inserted after the slash rewrite: JSON escapes are backslashes too.
        runner_argv[3:3] = ["--author", json.dumps(author, separators=(",", ":"))]
    return shlex.join(runner_argv)


def _start_delivery(
    argv: list[str], content: str, label: str, *, stdin_file: bool,
    task_id: Optional[str], agent: Any, origin_session_id: Optional[str] = None,
    route_reason: str = "canonical_title_fallback",
    profile_home: Path | None = None, author: Optional[dict] = None, wake: Any = "notify",
) -> str:
    from agent import durable_admission as da

    if da.admission_enabled():
        return _start_enforced_delivery(argv, content, label, stdin_file=stdin_file, task_id=task_id,
            agent=agent, origin_session_id=origin_session_id, route_reason=route_reason, profile_home=profile_home, author=author, wake=wake)
    if not da.observation_enabled():
        return _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=origin_session_id, route_reason=route_reason, profile_home=profile_home, author=author, wake=wake)
    observation = da.reserve_observed_effect(label, content)
    if observation is None:
        return _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=origin_session_id, route_reason=route_reason, profile_home=profile_home, author=author, wake=wake)
    effect = observation["effect"]
    key = effect["effect_id"]
    # The cross-process delivery ledger arbitrates dispatch, not an expiring
    # Work lease. Replay is decided before any authority call.
    with _delivery_ledger_lock(key):
        existing = _read_delivery_ledger(_delivery_ledger_path(key), key)
        if existing is not None:
            return json.dumps({"status": "delivered" if existing["state"] == "delivered" else "accepted",
                "to": label, "delivery": _public_delivery_receipt(existing),
                "detail": "Exact delivery already recorded; no resend or authority call."})
        identity = fence = None
        if observation["created"]:
            try:
                identity = da.admit_observed_effect(observation)
                if identity is not None:
                    fence = da._run_turn_identity_revalidation(identity)
            except Exception:
                logging.getLogger(__name__).debug('_start_delivery: optional operation failed', exc_info=True)
                identity = fence = None
                da.observation_gap(observation, "admission_failed")
        else:
            # Reservation survived without a dispatch ledger: no blind remote
            # admission retry. Legacy transport still owns its first dispatch.
            da.observation_gap(observation, "delivery_ambiguous")
        prepared = _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=effect["origin_session_id"],
            route_reason=route_reason, profile_home=profile_home, author=author, wake=wake, _observation=observation, _identity=identity,
            _fence=fence, _idempotency=key, _defer_spawn=True)
    if isinstance(prepared, str):
        return prepared
    command, dm_file, receipt = prepared
    return _spawn_delivery(command, label, dm_file=dm_file, receipt=receipt, task_id=task_id, agent=agent, wake=wake)


def _start_enforced_delivery(argv, content, label, *, stdin_file, task_id, agent,
                             origin_session_id, route_reason, profile_home=None, author=None, wake="notify"):
    from agent import durable_admission as da

    parent = da.current_admitted_turn()
    origin = str(origin_session_id or getattr(agent, "session_id", "") or "")
    carrier = da.current_effect_origin()
    mapped = (parent is not None and carrier is not None and carrier.session_id == origin
              and carrier.ingress_session_id == parent.session_id and carrier.event_id == parent.event_id
              and carrier.profile == da._active_profile())
    if parent is None or not origin or (parent.session_id != origin and not mapped) or not parent.event_id:
        return _err("Delivery authority refused: admitted input origin missing or mismatched.")
    request = {"tool": "message_agent", "destination": label,
               "content_sha256": da.content_sha256(content), "input_event_id": parent.event_id,
               "profile": da._active_profile()}
    scope = str(_hermes_root(Path(_agent_home(agent))))
    key = _canonical_sha256(["hermes.enforced-delivery.v1", scope, origin, request])
    with _delivery_ledger_lock(key):
        existing = _read_delivery_ledger(_delivery_ledger_path(key), key)
        if existing is not None:
            return json.dumps({"status": existing["state"], "to": label,
                "delivery": _public_delivery_receipt(existing),
                "detail": "Exact effect already recorded; no resend or authority call."})
        outcome = da.admit_execution(session_id=origin, request=request,
                                     event_id=da.execution_event_id(origin, request))
        if outcome.state != da.STATE_ADMITTED or not outcome.model_may_run:
            return _err("Delivery authority refused: " + str(outcome.reason_code))
        with da.admitted_turn_scope(outcome):
            prepared = _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
                task_id=task_id, agent=agent, origin_session_id=origin, route_reason=route_reason, profile_home=profile_home, author=author, wake=wake,
                _idempotency=key, _defer_spawn=True, _enforced=True)
    if isinstance(prepared, str):
        return prepared
    command, dm_file, receipt = prepared
    return _spawn_delivery(command, label, dm_file=dm_file, receipt=receipt, task_id=task_id, agent=agent, wake=wake)


def _live_delivery_eligible(agent, identity, observation, enforced, origin_session_id):
    from tools import bot_delivery_queue as queue

    return (identity is None and observation is None and not enforced
            and not origin_session_id and not queue.enabled(Path(_agent_home(agent))))


def _start_delivery_inner(
    argv: list[str],
    content: str,
    label: str,
    *,
    stdin_file: bool,
    task_id: Optional[str],
    agent: Any,
    origin_session_id: Optional[str] = None,
    route_reason: str = "canonical_title_fallback",
    profile_home: Path | None = None, author: Optional[dict] = None, wake: Any = "notify",
    _observation=None, _identity=None, _fence=None, _idempotency=None, _defer_spawn=False, _enforced=False,
) -> str:
    """Create a DM file and transfer its cleanup ownership to the runner."""
    turn_identity = _identity
    fence = _fence
    try:
        from agent.durable_admission import (
            admission_enabled,
            revalidate_current_turn_identity,
        )

        if admission_enabled():
            turn_identity, fence = revalidate_current_turn_identity()
    except Exception as exc:
        logging.getLogger(__name__).debug('_start_delivery_inner: optional operation failed', exc_info=True)
        return _err(f"Delivery authority refused: {type(exc).__name__}: {exc}")
    origin = str(origin_session_id or getattr(agent, "session_id", "") or "")
    if not origin:
        return _err("Delivery requires origin_session_id; no fallback session is available.")
    dm_file = _write_dm_file(content)
    from tools import bot_delivery_queue as queue
    # The live-owner protocol has a different receipt seam. It cannot bypass the
    # fork's durable worker, effect ledger or explicit destination-session binding.
    live_eligible = _live_delivery_eligible(agent, turn_identity, _observation, _enforced, origin_session_id)
    if profile_home is not None and live_eligible:
        try:
            record = _admit_live_dm(profile_home, dm_file, author)
        except LiveTargetUnavailableError as exc:
            return _settled_no_send(dm_file, exc)
        except Exception as exc:
            logging.getLogger(__name__).debug('_start_delivery_inner: optional operation failed', exc_info=True)
            return json.dumps({"status": "ambiguous", "delivery_id": _dm_delivery_id(dm_file),
                "error": f"Live delivery admission could not be confirmed: {exc}. Do not resend.",
                "evidence_file": dm_file})
        if record is not None:
            if wake == "none":
                # The admission is durable at the owner and nothing waits for its reply: the intent and
                # payload exist only for a waiting runner's same-id replays.
                _unlink_dm_file(_live_intent_file(dm_file))
                _unlink_dm_file(dm_file)
                return json.dumps(_with_target_state(dict(
                    status=record["status"], delivery_id=record["delivery_id"], to=label, expects_reply=False,
                    detail=_NOTIFY_ONLY_DETAIL), record, label))
            command = _delivery_command(argv, dm_file, stdin_file=False, profile_home=profile_home, author=author)
            notification = json.loads(_spawn_delivery(command, label, delivery_id=record["delivery_id"],
                                                      task_id=task_id, agent=agent, wake=wake))
            result = dict(status=record["status"], delivery_id=record["delivery_id"], to=label,
                          detail="Durably queued for the live Bot Chat owner. Do NOT wait or resend; finish your turn.")
            if notification.get("error"):
                result["notification_error"] = notification["error"]
            elif notification.get("process_id"):
                result["process_id"] = notification["process_id"]
                result["reply_delivery"] = notification.get("reply_delivery", "notification")
                if result["reply_delivery"] == "poll":
                    # Same runner, same stdout-borne reply (#101142): a non-push sender must get
                    # the poll instruction here too, not 'finish your turn'.
                    result["detail"] = f"Durably queued for the live Bot Chat owner. Do NOT resend. {notification['detail']}"
            return json.dumps(_with_target_state(result, record, label))
    try:
        origin = str(origin_session_id or getattr(agent, "session_id", "") or "")
        origin_reason = "explicit" if origin_session_id else "agent_session_fallback"
        if not origin:
            _unlink_dm_file(dm_file)
            return _err("Delivery requires origin_session_id; no fallback session is available.")
        if turn_identity is not None and origin != turn_identity.get("origin_session_id"):
            _unlink_dm_file(dm_file)
            return _err("Delivery authority refused: origin session rebind.")
        # The durable ledger is shared by all profile processes on one Hermes
        # install.  Scope idempotency to that install so unrelated test or
        # tenant homes that happen to reuse a session id never suppress a send.
        ledger_scope = str(_hermes_root(Path(_agent_home(agent))))
        authority_request = str((turn_identity or {}).get("request_key") or "")
        idempotency_key = hashlib.sha256(
            (ledger_scope + "\0" + origin + "\0" + authority_request + "\0" + label + "\0" + content).encode("utf-8")
        ).hexdigest()
        idempotency_key = _idempotency or idempotency_key
        receipt = _write_delivery_receipt(
            dm_file,
            origin_session_id=origin,
            origin_reason=origin_reason,
            route_reason=route_reason,
            label=label,
            idempotency_key=idempotency_key,
            turn_identity=turn_identity,
            fence=fence,
            observation=_observation,
            ledger_locked=_idempotency is not None, enforced_effect=_enforced,
            author=author, profile_home=profile_home,
        )
        if receipt.get("duplicate") and receipt.get("state") == "delivered":
            _unlink_dm_file(dm_file)
            _unlink_dm_file(dm_file + ".receipt.json")
            _unlink_dm_file(str(_delivery_envelope_path(dm_file)))
            return json.dumps({"status": "delivered", "to": label,
                               "delivery": _public_delivery_receipt(receipt),
                               "detail": "Delivery already persisted for this exact request; no duplicate spawn."})
        command = _delivery_command(argv, dm_file, stdin_file=stdin_file, profile_home=profile_home, author=author)
    except BaseException:
        _unlink_dm_file(dm_file)
        _unlink_dm_file(dm_file + ".receipt.json")
        raise
    if not stdin_file:
        from tools.bot_mode_dm_dispatch import prepare_queued_delivery
        queued = prepare_queued_delivery(argv, content, receipt, agent, dm_file, command, _idempotency)
        if isinstance(queued, str):
            return queued
        command, dm_file, receipt = queued
    if _defer_spawn:
        return command, dm_file, receipt
    return _spawn_delivery(
        command,
        label,
        dm_file=dm_file,
        receipt=receipt,
        task_id=task_id,
        agent=agent,
        wake=wake,
    )


_NOTIFY_ONLY_DETAIL = "Sent as notify-only: no reply comes back and nothing will wake you. Do not resend."
# What a sender should tell its user when the teammate's turn is parked on that teammate's own user.
_BLOCKED_DETAIL = {
    "waiting_approval": "is waiting for its user to approve a command in its own Bot Chat",
    "waiting_clarify": "is waiting for its user to answer a question in its own Bot Chat",
    "paused": "is not starting new turns right now (its gateway is restarting or updating)",
}


def _with_target_state(result: dict, record: dict, label: str) -> dict:
    """Carry the owner's live ``target_state`` into an ack; a blocked teammate is said so plainly."""
    state = record.get("target_state")
    if state:
        result["target_state"] = state
    if state in _BLOCKED_DETAIL:
        result["detail"] += (f" {label} {_BLOCKED_DETAIL[state]}, so its reply comes only after that: "
                             "tell your user now.")
    return result


def _spawn_delivery(command: str, label: str, *, dm_file: Optional[str] = None, delivery_id: Optional[str] = None,
                    task_id: Optional[str], agent: Any, wake: Any = "notify",
                    receipt: Optional[dict[str, str]] = None,
                    completion_chars: int = REPLY_COMPLETION_CHARS) -> str:
    """Launch the cleanup-owning runner and transfer file ownership on ack. ``dm_file``
    is None for relay deliveries (the waiter watches a reply file; envelope artifacts
    are owned/swept by ``tools/bot_relay.py``), which pass the envelope id as ``delivery_id``
    instead. The ack is ``queued`` + ``delivery_id`` like the live-owner branch: hand-off
    to a background process, never a delivery receipt. A list ``wake`` collects the runner for
    one fan-out process to join (it owns ``dm_file`` from here); ``"none"`` runs it unwatched."""
    if isinstance(wake, list) and not (receipt and receipt.get("queue_id")):
        delivery_id = delivery_id or (_dm_delivery_id(dm_file) if dm_file else "")
        wake.append(dict(command=command, label=label, dm_file=dm_file, delivery_id=delivery_id))
        return json.dumps({"status": "queued", "delivery_id": delivery_id, "to": label})
    transferred = False
    queued = bool(receipt and receipt.get("queue_id"))
    def queued_without_listener(detail):
        return json.dumps({"status": receipt.get("state", "queued"), "to": label,
                           "delivery_id": receipt.get("delivery_id") or receipt["queue_id"],
                           "delivery": _public_delivery_receipt(receipt),
                           "detail": "Request saved; destination has not received it yet. " + detail})
    if queued:
        # The durable worker owns both execution and completion notification.
        # A sender-owned notify_on_complete waiter would make one-shot exit
        # hold the source turn lock while waiting for a reply to that same
        # source: Chief -> Staff -> Chief then stalls until the linger expires.
        # The no-listener response is already supported; the queue/inbox is
        # the receipt, not a transient process in this interpreter.
        return queued_without_listener(
            "The durable worker owns delivery. Finish this turn; completion "
            "status will arrive in the updates inbox and the result is retained "
            "in the delivery receipt. Do not resend or poll."
        )
    try:
        from tools.terminal_tool import terminal_tool

        raw = terminal_tool(command, background=True, notify_on_complete=wake != "none", task_id=task_id,
                            workdir=str(Path(__file__).resolve().parent.parent), _host_local=True,
                            _completion_output_chars=completion_chars)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
        proc_id = parsed.get("session_id") or ""
        if parsed.get("error"):
            if queued:
                return queued_without_listener("Completion listener unavailable; inspect the updates inbox.")
            _release_unspawned_delivery(dm_file)
            return _err(f"Delivery to {label} failed to start: {parsed['error']}")
        if parsed.get("status") == "pending_approval":
            # terminal_tool's approval gate answers with an EMPTY error and no session_id: the runner
            # never launched because nobody in this turn could approve its command.
            _release_unspawned_delivery(dm_file)
            return _err(f"Delivery to {label} failed to start: its command needs terminal approval that nobody "
                        "in this turn can grant" + (", so nothing was sent. Approve it (or add it to "
                                                    "command_allowlist) and send again." if dm_file else "."))
        if not proc_id:
            if queued:
                return queued_without_listener("Completion listener unavailable; inspect the updates inbox.")
            _release_unspawned_delivery(dm_file)
            return _err(f"Delivery to {label} failed to start: no process id returned")
        # From here the background runner owns the file (removed after the consumer finishes).
        transferred = True
        if wake == "none":
            return json.dumps({"status": "queued", "delivery_id": delivery_id or (_dm_delivery_id(dm_file) if dm_file
                               else ""), "to": label, "expects_reply": False, "detail": _NOTIFY_ONLY_DETAIL})
        if parsed.get("notify_on_complete") is False:
            # terminal_tool refused the completion promise: this session (api_server, one-shot
            # runner) cannot receive an async completion, so the recipient's reply would never
            # be injected here (#101142). Say so and name the return path the surface supports.
            detail = (f"Message handed to a background delivery process for {label}, but THIS session "
                      "cannot receive completion notifications, so the reply will NOT arrive on its own. "
                      f"Before ending your turn, retrieve the outcome with process(action='wait', "
                      f"session_id='{proc_id}') — its output is the reply (relay it, attributed to that "
                      "agent) or the delivery failure (report it; the message was NOT delivered); "
                      "if wait returns status=timeout, call wait again until the process exits.")
            if _persist_reply_when_done(proc_id, agent):
                detail += (" Its outcome is also saved into this session's transcript as a delivery row "
                           "when the process exits, so it survives even if the turn ends first.")
        else:
            detail = (f"Message queued for {label}: this acknowledges the hand-off to a "
                      "background delivery process, not a delivery receipt — do NOT wait or poll. "
                      "Finish your turn now; that process's completion notification carries the "
                      "delivery outcome — the reply (relay it then, attributed to that agent) or "
                      "the delivery failure (report it; the message was NOT delivered).")
        return json.dumps({
            "status": "queued",
            "delivery_id": delivery_id or (_dm_delivery_id(dm_file) if dm_file else ""),
            "to": label,
            "reply_delivery": "poll" if parsed.get("notify_on_complete") is False else "notification",
            "detail": detail,
            "process_id": proc_id,
            "queued_at": int(time.time()),
            **({"delivery": _public_delivery_receipt(receipt)} if receipt else {}),
        })
    except Exception as exc:
        if queued:
            return queued_without_listener("Completion listener unavailable; inspect the updates inbox.")
        _release_unspawned_delivery(dm_file)
        logger.error("message_agent delivery spawn failed: %s", exc, exc_info=True)
        return _err(f"Delivery to {label} could not be started: {exc}")
    finally:
        if dm_file and not transferred:
            _unlink_dm_file(dm_file)


def _persist_reply_when_done(proc_id: str, agent: Any) -> bool:
    """#101142 durable leg. A non-push sender (api_server, one-shot runner) gets no completion
    notification, so once the tracked runner exits its stdout — the recipient's reply or the
    delivery failure — is appended to the sender's session transcript as a DELIVERY row
    (``display_kind="process_complete"``, the shape push surfaces persist for the same
    completion; mirrors gateway.wake.persist_delegation_delivery). A sender that already read
    the outcome via process(action='wait'/'log') is not told twice. Returns False (nothing
    armed) when the sender has no session transcript or the process is not tracked here."""
    from tools.process_registry import process_registry

    db, session_id = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
    proc = process_registry.get(proc_id)
    if proc is None or not session_id or not callable(getattr(db, "append_message", None)):
        return False

    def _run() -> None:
        from tools.process_registry_notifications import (
            format_process_notification, process_completion_display_text,
        )

        proc._completion_event.wait()
        if process_registry.is_completion_consumed(proc_id):
            return
        evt = {"type": "completion", "session_id": proc_id, **process_registry._exit_snapshot(proc, "exited")}
        try:
            db.append_message(session_id, "user", content=format_process_notification(evt),
                              display_kind="process_complete",
                              display_metadata={"display_text": process_completion_display_text([evt])})
        except Exception as exc:
            logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
            logger.warning("message_agent: could not persist the reply of %s into session %s: %s",
                           proc_id, session_id, exc)

    threading.Thread(target=_run, name=f"message-agent-reply-{proc_id}", daemon=True).start()
    return True


def _wait_reply_main(reply_path: str, label: str, budget_seconds: str) -> int:
    """The relay reply waiter (``tools/bot_relay.waiter_command``): block until the sender-side
    reply file exists, print it as the completion notification the sender wakes on, exit 1 on a
    delivery error or when the budget runs out. Stdlib only: this runs as a background process
    from any bot turn, and the sender's completion notification is exactly its stdout."""
    try:
        deadline = time.time() + float(budget_seconds)
    except ValueError:
        return 2
    while time.time() < deadline:
        if os.path.exists(reply_path):
            with open(reply_path, encoding="utf-8-sig") as fh:
                d = json.load(fh)
            if d.get("error"):
                # Typed reason code rides ahead of the free text so the sender can branch on it
                # without parsing provider prose. See #93091.
                code = str(d.get("reason") or "").strip()
                tag = f" [reason: {code}]" if code else ""
                print(f"Delivery to {label} failed{tag}: {d['error']}")
                return 1
            print(f"Reply from {label}:")
            print(d.get("reply") or "(empty reply)")
            return 0
        # 250ms cadence: stat is cheap and a longer sleep is pure dead air.
        time.sleep(0.25)
    print(f"No reply from {label} within {budget_seconds}s. The message may still be delivered when "
          "the Desktop reconnects; do not resend blindly.")
    return 1


def _delivery_main(args: list[str]) -> int:
    """Runner entry for the argv ``_delivery_command`` and ``bot_relay.waiter_command`` build.
    Malformed argv exits 2 without touching the DM file."""
    if args[:1] == ["--wait-reply"]:
        return _wait_reply_main(*args[1:]) if len(args) == 4 else 2
    if args[:1] == ["--fan-out"]:
        from tools.bot_mode_dm_coordination import run_fan_out

        return run_fan_out(args[1]) if len(args) == 2 else 2
    parsed = _runner_argv(args)
    if parsed is None:
        return 2
    author_json, mode, dm_file, argv = parsed
    author = None
    if author_json is not None:
        from agent.turn_author import parse_turn_author

        author = parse_turn_author(author_json)
        if author is None:
            return 2
    try:
        profile_home = None
        if len(argv) >= 2 and argv[0] == "--profile-home":
            profile_home, argv = Path(argv[1]), argv[2:]
        return _run_delivery(argv, dm_file, stdin_file=mode == "stdin", profile_home=profile_home, author=author)
    except Exception as exc:
        # Every refusal ships a typed reason on stdout so the completion notification carries it
        # back to the sender (#93091): 'target_busy' from the queue's bounded wait, otherwise the
        # same vocabulary-guarded classification the relay lane applies.
        logging.getLogger(__name__).debug('Recoverable operation failed', exc_info=True)
        # back to the sender (#93091), with the same vocabulary-guarded classification the relay
        # lane applies.
        from tools.bot_failure_reasons import delivery_failure_reason

        print(json.dumps({"error": str(exc), "reason": delivery_failure_reason(exc)}))
        return 1


# agent-context helpers (mirror system_prompt.py's resolution)


def _agent_home(agent: Any) -> str:
    """The calling agent's OWN home (session-db derived), not ambient env."""
    with contextlib.suppress(Exception):
        db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
        if db_path:
            return str(Path(db_path).parent)
    return _default_home()


def _session_title(agent: Any) -> str:
    title = str(getattr(agent, "_session_title_hint", "") or "").strip()
    if title:
        return title
    with contextlib.suppress(Exception):
        sdb, sid = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
        if sdb and sid:
            return str(sdb.get_session_title(sid) or "").strip()
    return ""


if __name__ == "__main__":  # pragma: no cover - exercised as a background process
    # Spawned as a script with the sender's sys.executable, which under PM is the bare store
    # interpreter (dependencies are activated in-process, never inherited), so boot like every
    # entry point before the lazy Hermes imports. Run as a path, sys.path[0] is tools/.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Only the delivery lane boots: the reply waiter is stdlib only and its stdout is the
    # sender's wake-up, and any other argv exits 2 before a Hermes import.
    if sys.argv[1:2] == ["--run-delivery"]:
        try:
            import hermes_bootstrap
        except (Exception, SystemExit) as exc:
            # A pinned live intent means the sender may already have admitted this DM and was
            # told not to resend; a bare repair hint would read as "NOT delivered". Without an
            # intent nothing was handed over, so the plain failure is the truth. A relaunched
            # child (Windows) already printed its own outcome.
            booted = getattr(exc, "relaunched", False) or (isinstance(exc, SystemExit) and not exc.code)
            parsed = _runner_argv(sys.argv[1:])
            if not booted and parsed and os.path.exists(_live_intent_file(parsed[2])):
                print(_live_outcome_unknown(parsed[2], "the delivery runner could not activate "
                                                       "Hermes dependencies (see stderr)"))
            raise
    raise SystemExit(_delivery_main(sys.argv[1:]))
