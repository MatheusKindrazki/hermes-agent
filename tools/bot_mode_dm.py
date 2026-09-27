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
                "Send a message to ANOTHER agent (teammate) on this install, or to an "
                "agent on a registered peer gateway. This is FIRE-AND-FORGET and "
                "asynchronous, like texting: it validates the target against the live "
                "roster, delivers your message into that agent's own Bot Chat with your "
                "attribution automatically prefixed, and returns immediately with a "
                "dispatch acknowledgement — status queued plus a delivery_id (the hand-off to a background "
                "delivery process, not a delivery receipt). It does NOT return their reply and you must "
                "not wait or poll for one — send it, finish your turn, and that process's "
                "completion notification wakes you with the outcome: their reply, or the "
                "delivery failure — unless the ack returns reply_delivery=\"poll\", in which case "
                "follow its process(action=\"wait\") instruction before ending the turn. COMPOSE the message yourself: write what YOU want to say to "
                "that agent (lead with the point; include the concrete ask or result). "
                "Never paste the user's words verbatim — paraphrase the actionable "
                "substance, and keep private 1:1 chat content private. Message one "
                "clearly relevant teammate when it genuinely helps the user's goal; "
                "don't fan out to several agents unless the user explicitly asked. "
                "Use the teammate roster in your system prompt (names + roles) to pick "
                "the right recipient; targets: a teammate name (e.g. 'researcher'), "
                "'<peer>/<agent>' for an agent on a registered peer gateway "
                "(e.g. 'spark/researcher', or just '<peer>' for the peer's main agent), "
                "or an agent on another connected machine from your roster (use "
                "'<handle>@<connection>' if the same handle exists on several)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Who to message: a teammate profile name from your roster "
                            "('researcher', 'hermes' for the default agent), or "
                            "'<peer>' / '<peer>/<agent>' for a registered peer gateway."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The message YOU composed for that agent (max "
                            f"{MESSAGE_MAX_CHARS} chars). Do not include the "
                            "'Message from …' prefix — it is added automatically."
                        ),
                    },
                },
                "required": ["target", "message"],
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
    """Map a target to a local profile FOLDER id: 'hermes' → 'default'; an exact folder id
    (case-insensitive); else — when ``root`` is given — a friendly name or its Desktop @-slug
    (profile.yaml ``display_name`` / Bot Mode title: 'Scribe', '@scribe', 'Dr. Foo' → 'foo').
    Ambiguous friendly names resolve to None so a DM never lands on the wrong bot (#100671)."""
    want = target.strip().lower()
    if not want:
        return None
    if want == "hermes":
        return "default" if "default" in roster else None
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


def message_agent_tool(
    target: str = "",
    message: str = "",
    task_id: Optional[str] = None,
    origin_session_id: Optional[str] = None,
    agent: Any = None,
) -> str:
    """Deliver ``message`` to ``target``'s Bot Chat. Returns a JSON ack/error.
    ``agent`` is the calling AIAgent — used for the Bot Chat gate and sender identity."""
    if not getattr(agent, "_bot_mode_protocol", True):
        return _err("message_agent is disabled for this agent session. Do not retry.")
    home = _agent_home(agent)
    try:
        from tools.bot_mode_probe import (
            BOT_CHAT_TITLE, _display_name, _handle, _hermes_root, _peers, _profile_name as _self_profile_name,
            _roster, is_bot_mode_managed,
        )
        from tools.bot_relay import BOT_CHAT_TURN_ARGS, _hermes_cli

        if _session_title(agent) != BOT_CHAT_TITLE:
            return _err("message_agent is only available in a Bot Mode 'Bot Chat' session. "
                        "This session is not one; do not retry.")
        if not is_bot_mode_managed(home):
            return _err("This install is not Bot-Mode-managed (no bot roster); "
                        "message_agent is unavailable. Do not retry.")
    except Exception as exc:  # pragma: no cover — defensive
        return _err(f"Bot Mode gate check failed: {exc}")

    root, me = _hermes_root(Path(home)), _self_profile_name(Path(home))
    roster_homes = dict(_roster(root))
    roster = list(roster_homes)
    peers = _peers(root)
    teammates = [_handle(n) for n in roster if n != me]

    def _roster_err(msg: str) -> str:
        return _err(msg, roster=teammates, peers=peers)

    body = str(message or "").strip()
    if not body:
        return _err("message is required — compose what you want to say to that agent.")
    if len(body) > MESSAGE_MAX_CHARS:
        return _err(f"message too long ({len(body)} chars > {MESSAGE_MAX_CHARS}). "
                    "Send the essentials; share large content as a file path instead.")

    raw_target = str(target or "").strip().lstrip("@")
    if not raw_target:
        return _roster_err("target is required.")
    # Sender signature: the friendly name when the bot has one (#89720); the @handle stays the routing alias.
    content = f"Message from 🤖 {_display_name(me, roster_homes.get(me, Path(home)))} (@{_handle(me)}): " + body
    delivery = dict(task_id=task_id, agent=agent, origin_session_id=origin_session_id)
    # Attribution for the recipient's memory hooks; the text prefix above stays the human-facing signature.
    author = {"id": f"bot:{me}", "name": _handle(me), "is_bot": True}

    # Peer target: '<peer>/<agent>' or a bare registered peer name.
    peer_match = _PEER_TARGET_RE.match(raw_target)
    if peer_match or raw_target.lower() in peers:
        peer_name, peer_profile = peer_match.groups() if peer_match else (raw_target.lower(), None)
        if peer_name not in peers:
            return _roster_err(f"No registered peer named '{peer_name}'.")
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
        # this interpreter; _delivery_lock/_local_delivery_home match argv[0] by basename,
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
        relayed = _try_relay_delivery(root, raw_target, content, me, task_id=task_id, agent=agent)
        if relayed is not None:
            return relayed
    # Local teammate — folder id, or a friendly name / Desktop @-slug ('Scribe', 'Dr. Foo').
    resolved = _resolve_local_name(raw_target, roster, root)
    is_local_shape = bool(_LOCAL_TARGET_RE.match(raw_target))
    if resolved is None and not is_local_shape and "@" not in raw_target:
        return _roster_err(f"Invalid target: {raw_target!r}.")
    if resolved is None or resolved == me:
        # Unknown locally, or same-name target on ANOTHER connection (this gateway's 'default'
        # messaging the cloud 'default'): every Desktop-connected gateway is reachable via the
        # relay roster, so try that before reporting a resolution failure / self-message.
        relay_refusal = _relay_enforce_refusal()
        if relay_refusal is not None:
            return relay_refusal
        relayed = _try_relay_delivery(root, raw_target, content, me, task_id=task_id, agent=agent)
        if relayed is not None:
            return relayed
        if resolved == me:
            return _err("You can't message yourself. Pick a teammate from the roster.")
        return _roster_err(f"No teammate named '{raw_target}' on this install, on a connected "
                           "machine, or on a registered peer. Pick a name from the roster "
                           "(roles are listed in your system prompt).")
    exact_session, route_reason = _resolve_target_bot_session(resolved, origin_session_id)
    if origin_session_id and not exact_session:
        return _err(f"Delivery origin session {origin_session_id!r} is not an eligible Bot Chat "
                    f"for profile '{resolved}' ({route_reason}); refusing title fallback.")
    argv = [_hermes_cli(), "-p", resolved]
    if exact_session:
        argv += ["--resume", exact_session]
    argv += list(BOT_CHAT_TURN_ARGS)
    return _start_delivery(argv, content, f"@{_handle(resolved)}",
                           stdin_file=False, profile_home=roster_homes[resolved], author=author, route_reason=route_reason, **delivery)


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
                        task_id: Optional[str], agent: Any) -> Optional[str]:
    """Cross-connection delivery via the Desktop relay; None when the target doesn't
    resolve against the relay roster. The envelope is queued on disk for the Desktop
    to drain; a background waiter is spawned immediately so the relayed reply wakes
    the sender through the standard completion-notification path."""
    try:
        from tools.bot_mode_probe import _handle, local_taken_forms
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
            envelope = enqueue_envelope(root, target=match, message=content, sender_profile=me, sender_handle=_handle(me))
        except EnvelopeRefusedError as exc:
            # Fail fast: target definitively offline — nothing was queued.
            # Structured refusal so the agent can distinguish it from a resolution error ('runtime_offline'
            # per the #93091 reason enum).
            return json.dumps({"error": str(exc), "reason": exc.reason})
        label = f"@{match['handle']} on {match['connection_label'] or match['connection_id']}"
        raw = _spawn_delivery(waiter_command(root, envelope), label, delivery_id=envelope["id"], task_id=task_id, agent=agent)
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
    cutoff = (time.time() if now is None else now) - max_age_hours * 3600
    temp_root = Path(tempfile.gettempdir())
    locations = [(temp_root, "hermes-dm-*.txt"), (temp_root, "hermes-relay-dm-*.txt")]
    with contextlib.suppress(OSError):
        dm_dir = _dm_dir()
        locations.append((dm_dir, "*.txt"))
        # Live-delivery intents (``<dm file>.live.json``, message plaintext included) outlive
        # their runner on purpose — a retry replays the same delivery id from them — so the
        # orphans of runners that never settled are swept here too.
        locations.append((dm_dir, "*.live.json"))
    from tools.bot_relay import unlink_files_older_than

    return sum(unlink_files_older_than(d, pattern, cutoff) for d, pattern in locations)


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


@contextlib.contextmanager
def _delivery_ledger_lock(idempotency_key: str):
    """Cross-process lock whose ownership dies with the process, not the file."""
    path = _delivery_ledger_path(idempotency_key).with_suffix(".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("delivery_lock_unsafe")
        if fcntl is None:
            import msvcrt  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("delivery idempotency lock busy")
                    time.sleep(0.01)
        yield
    finally:
        try:
            if fcntl is None:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[name-defined]  # pragma: no cover
                except Exception:
                    pass
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_delivery_ledger(path: Path, idempotency_key: str) -> Optional[dict[str, Any]]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("delivery_ledger_unsafe")
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                document = json.load(stream)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("delivery_ledger_unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(document, dict) or document.get("idempotency_key") != idempotency_key:
        raise RuntimeError("delivery_ledger_rebind")
    if not isinstance(document.get("delivery_id"), str) or not document["delivery_id"]:
        raise RuntimeError("delivery_ledger_invalid")
    return document


def _write_delivery_receipt(
    dm_file: str, *, origin_session_id: str, origin_reason: str, route_reason: str,
    label: str, idempotency_key: str, turn_identity: Optional[Mapping[str, Any]] = None,
    fence: Optional[Mapping[str, Any]] = None,
    observation: Optional[dict] = None, ledger_locked: bool = False, enforced_effect: bool = False,
    author: Optional[dict] = None, profile_home: Path | None = None,
) -> dict[str, Any]:
    """Persist the accepted-state receipt before spawning a child process.

    A terminal PID only proves that the Desktop accepted a spawn request.  The
    sidecar is intentionally content-free and gives retries a stable delivery
    identity tied to the originating Bot Chat session.  The child updates this
    same record to a terminal state in :func:`_run_delivery`.
    """
    ledger = _delivery_ledger_path(idempotency_key)
    with (contextlib.nullcontext() if ledger_locked else _delivery_ledger_lock(idempotency_key)):
        existing = _read_delivery_ledger(ledger, idempotency_key)
        if isinstance(existing, dict) and existing.get("idempotency_key") == idempotency_key:
            existing["duplicate"] = True
            _atomic_json(Path(dm_file + ".receipt.json"), existing)
            if isinstance(existing.get("turn_identity"), dict):
                _atomic_json(_delivery_envelope_path(dm_file), {
                    "schema": DELIVERY_ENVELOPE_SCHEMA,
                    "delivery_id": existing["delivery_id"],
                    "request_key": existing["turn_identity"]["request_key"],
                    "turn_identity_sha256": _canonical_sha256(existing["turn_identity"]),
                    "accepted_at": int(existing["accepted_at"]),
                })
            return existing
        delivery_id = str(uuid.uuid4())
        record = {
            "schema": "hermes.delivery/v1", "delivery_id": delivery_id,
            "origin_session_id": str(origin_session_id or ""),
            "origin_reason": origin_reason, "route_reason": route_reason, "target": label,
            "idempotency_key": idempotency_key, "state": "accepted",
            "accepted_at": int(time.time()),
        }
        if author:
            record["turn_author"] = dict(author)
        if profile_home is not None:
            record["profile_home"] = str(profile_home)
        if observation is not None or enforced_effect:
            from agent.durable_admission import new_release_event_id
            record["release_event_id"] = new_release_event_id()
        if observation is not None:
            record["observation"] = observation
        if enforced_effect:
            record["enforced_effect"] = True
        if turn_identity is not None:
            record["turn_identity"] = dict(turn_identity)
            record["fence"] = dict(fence or {})
        path = Path(dm_file + ".receipt.json")
        _atomic_json(path, record)
        _atomic_json(ledger, record)
        if turn_identity is not None:
            _atomic_json(_delivery_envelope_path(dm_file), {
                "schema": DELIVERY_ENVELOPE_SCHEMA,
                "delivery_id": delivery_id,
                "request_key": turn_identity["request_key"],
                "turn_identity_sha256": _canonical_sha256(turn_identity),
                "accepted_at": record["accepted_at"],
            })
        return record


def _update_delivery_receipt(dm_file: str, state: str, *, ack: Optional[dict[str, Any]] = None,
                             ack_row_validated: bool = False) -> None:
    try:
        record = json.loads(Path(dm_file + ".receipt.json").read_text(encoding="utf-8"))
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
        observation_gap(record.get("observation"), "ledger_failed")


def _update_delivery_receipt_inner(dm_file: str, state: str, *, ack=None, ack_row_validated=False):
    path = Path(dm_file + ".receipt.json")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
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
        observation_gap(observation, "spool_failed")
        return
    try:
        record["release_state"] = "requested"
        _atomic_json(path, record)
        ledger = _delivery_ledger_path(record["idempotency_key"])
        _atomic_json(ledger, record)
        ledger_hash = hashlib.sha256(ledger.read_bytes()).hexdigest()
    except Exception:
        observation_gap(observation, "ledger_failed")
        return
    try:
        release = release_observed_effect(record["turn_identity"], record["release_event_id"])
    except Exception:
        observation_gap(observation, "release_ambiguous")
        return
    try:
        spool_document = json.loads(spool.read_text(encoding="utf-8"))
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
        observation_gap(observation, "storage_failed")


def _emit_delivery_shadow_receipt(
    record: dict[str, Any], state: str, *, ack_bytes: Optional[str] = None
) -> Optional[Path]:
    """Observe a terminal DM effect only with an upstream-validated fence."""
    if os.environ.get("HERMES_KERNEL_SHADOW_PRODUCER_ENABLED", "0") != "1":
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
        record = json.loads(path.read_text(encoding="utf-8"))
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


def _delivery_runtime_env() -> dict[str, str]:
    """Keep internal Hermes CLI children on the runner's selected release.

    Terminal tools intentionally remove Hermes-owned PYTHONPATH entries. The
    delivery runner restores its own source only for its internal transport;
    otherwise a borrowed interpreter can import an older editable installation.
    """
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    entries = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep)
               if entry and entry != root]
    env["PYTHONPATH"] = os.pathsep.join([root, *entries])
    return env


def _run_local_turn(argv: list[str], dm_file: str, *, env: Optional[dict[str, str]] = None) -> int:
    """One Bot Chat turn via ``--query-file`` (plus one policy-gated retry); re-emits
    the transport's streams and returns its exit code. Transient failures re-run the
    same session; a context_overflow re-run lets the retried turn's pre-API compaction
    compact the transcript first (no fresh session is ever minted). Auth/quota/config never retry."""

    def _turn(turn_env=env):
        return subprocess.run([*argv, "--query-file", dm_file], check=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=turn_env)

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


def _dm_delivery_id(dm_file: "str | os.PathLike") -> str:
    """One delivery id per DM file: the dispatch ack, the live-owner intent and every retry
    of the runner derive it the same way, so the sender can correlate all of them."""
    return hashlib.sha256(str(Path(dm_file).resolve()).encode()).hexdigest()


def _admit_live_dm(profile_home: Path | None, dm_file: str, author: Optional[dict] = None) -> dict | None:
    """Pin intent before admission; retries may inspect, never change transport."""
    from tools.bot_live_delivery import deliver_to_live_owner, find_canonical_live_owner, read_delivery_result
    from utils import fsync_directory

    intent: dict[str, Any]
    intent_path = Path(dm_file + ".live.json")
    if intent_path.exists():
        intent = json.loads(intent_path.read_text(encoding="utf-8-sig"))
    else:
        assert profile_home is not None
        owner = find_canonical_live_owner(profile_home)
        if owner is None:
            return None
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
    home = intent["owner"]["profile_home"]
    record = read_delivery_result(home, intent["delivery_id"])
    if record is None:
        record = deliver_to_live_owner(home, intent["owner"], intent["message"],
                                       delivery_id=intent["delivery_id"], author=intent.get("author"))
    return record


def _wait_live_dm(home: str, delivery_id: str, *, dm_file: "str | os.PathLike | None" = None) -> int:
    from tools.bot_live_delivery import await_delivery

    record = await_delivery(home, delivery_id, _LIVE_WAIT_SECONDS)
    status = record["status"] if record else "ambiguous"
    payload = {key: record[key] for key in ("reply", "error", "reason") if record and record.get(key)}
    payload.update(status=status, delivery_id=delivery_id)
    if status in ("queued", "claimed", "ambiguous"):
        payload["detail"] = "Delivery remains pending or its outcome is unknown. Do not resend; receipt is retained."
    elif status == "settled" and dm_file is not None:
        # The intent carries the message plaintext so a retry can replay the SAME delivery id;
        # once the owner settled it nothing retries, so it goes along with the dm file (same
        # plaintext) — the live branch returns before _run_delivery's own unlink.
        _unlink_dm_file(str(dm_file) + ".live.json")
        _unlink_dm_file(str(dm_file))
    print(json.dumps(payload))
    return 0 if status in ("settled", "queued", "claimed") else 1


def _local_delivery_home(argv: list[str]) -> Path | None:
    cli = (argv[0] if argv else "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if len(argv) < 3 or cli not in ("hermes", "hermes.exe") or argv[1] != "-p":
        return None
    from tools.bot_mode_probe import _hermes_root, _roster

    return dict(_roster(_hermes_root(Path(_default_home())))).get(argv[2])


def _run_delivery(argv: list[str], dm_file: str, *, stdin_file: bool,
                  profile_home: Path | None = None, author: Optional[dict] = None,
                  lock_held: bool = False, strict_authority: bool = False) -> int:
    """Route to the live owner before attempting a CLI transport. Live deliveries
    retain their intent/payload and immutable receipt; only CLI/peer payloads are
    removed after consumption. The CLI turn window holds the profile lock, so two
    deliveries into one profile queue; a bounded wait ends in a 'target_busy' refusal.
    ``author`` rides to the child as HERMES_TURN_AUTHOR; ``hermes peer dm`` forwards it in the request body.

    Local (query-file) turns get one policy-gated retry (#93091 item 5): transient failures re-run the same
    session; a context_overflow re-run lets the retried turn's pre-API compaction pass compact the Bot Chat
    transcript first (agent/conversation_loop.py) — the sanctioned compression lever; no fresh session is
    ever minted. Auth/quota/config failures never retry. Peer transports (stdin mode) retry on their own
    gateway's deliver path, not here.
    """
    returncode = 1
    keep_live_intent = False
    record = {}
    structured = False
    ack: Optional[dict[str, Any]] = None
    ack_row_validated = False
    try:
        receipt_path = Path(dm_file + ".receipt.json")
        try:
            record = json.loads(receipt_path.read_text(encoding="utf-8"))
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
        if not structured and not record.get("observation") and not record.get("enforced_effect"):
            # Existing fork receipts pin CLI/queue transport. Only an unpinned
            # upstream live intent may route to the live Desktop owner here.
            if not stdin_file and not record:
                home = profile_home or _local_delivery_home(argv)
                if home is not None or Path(dm_file + ".live.json").exists():
                    try:
                        live_record = _admit_live_dm(home, dm_file, author)
                    except Exception as exc:
                        keep_live_intent = True
                        print(json.dumps({"status": "ambiguous", "delivery_id": _dm_delivery_id(dm_file),
                            "error": f"Live admission outcome unknown: {exc}. Do not resend.",
                            "evidence_file": dm_file}))
                        return 1
                    if live_record is not None:
                        keep_live_intent = True
                        return _wait_live_dm(live_record["profile_home"], live_record["delivery_id"], dm_file=dm_file)
            with (contextlib.nullcontext() if lock_held else _delivery_lock(argv, stdin_file=stdin_file)):
                if not stdin_file:
                    returncode = _run_local_turn(argv, dm_file, env=env)
                else:
                    with open(dm_file, "r", encoding="utf-8-sig") as stream:
                        returncode = subprocess.run(argv, input=stream.read().encode("utf-8"), check=False, env=env).returncode
                return returncode
        transport_argv = list(argv)
        if structured:
            try:
                fresh_fence = _revalidate_delivery_identity(record)
                if (
                    fresh_fence.get("fence_valid") is not True
                    or fresh_fence.get("identity_sha256") != _canonical_sha256(record["turn_identity"])
                ):
                    raise ValueError("delivery fence invalid")
            except Exception:
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
                message = Path(dm_file).read_text(encoding="utf-8")
                Path(dm_file).write_text(
                    _encode_delivery_query(
                        json.loads(_delivery_envelope_path(dm_file).read_text(encoding="utf-8")),
                        message,
                    ),
                    encoding="utf-8",
                )
                os.chmod(dm_file, 0o600)
        with (contextlib.nullcontext() if lock_held else _delivery_lock(argv, stdin_file=stdin_file)):
            if stdin_file:
                # Keep the file open until the transport exits; cleanup occurs
                # after subprocess.run returns, not merely after stdin reaches EOF.
                with open(dm_file, "r", encoding="utf-8") as stream:
                    proc = subprocess.run(
                        transport_argv, stdin=stream, check=False, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=env,
                    )
                returncode = proc.returncode
            else:
                proc = subprocess.run(
                    [*transport_argv, "--query-file", dm_file],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8", errors="replace", env=env,
                )
                returncode = proc.returncode
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
                        env=retry_turn_env(env),
                    )
                    returncode = proc.returncode
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
            return returncode
    finally:
        state = "delivered" if returncode == 0 and (ack is not None or not structured) else "failed"
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
    profile_home: Path | None = None, author: Optional[dict] = None,
) -> str:
    from agent import durable_admission as da

    if da.admission_enabled():
        return _start_enforced_delivery(argv, content, label, stdin_file=stdin_file, task_id=task_id,
            agent=agent, origin_session_id=origin_session_id, route_reason=route_reason, profile_home=profile_home, author=author)
    if not da.observation_enabled():
        return _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=origin_session_id, route_reason=route_reason, profile_home=profile_home, author=author)
    observation = da.reserve_observed_effect(label, content)
    if observation is None:
        return _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=origin_session_id, route_reason=route_reason, profile_home=profile_home, author=author)
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
                identity = fence = None
                da.observation_gap(observation, "admission_failed")
        else:
            # Reservation survived without a dispatch ledger: no blind remote
            # admission retry. Legacy transport still owns its first dispatch.
            da.observation_gap(observation, "delivery_ambiguous")
        prepared = _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=effect["origin_session_id"],
            route_reason=route_reason, profile_home=profile_home, author=author, _observation=observation, _identity=identity,
            _fence=fence, _idempotency=key, _defer_spawn=True)
    if isinstance(prepared, str):
        return prepared
    command, dm_file, receipt = prepared
    return _spawn_delivery(command, label, dm_file=dm_file, receipt=receipt, task_id=task_id, agent=agent)


def _start_enforced_delivery(argv, content, label, *, stdin_file, task_id, agent,
                             origin_session_id, route_reason, profile_home=None, author=None):
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
                task_id=task_id, agent=agent, origin_session_id=origin, route_reason=route_reason, profile_home=profile_home, author=author,
                _idempotency=key, _defer_spawn=True, _enforced=True)
    if isinstance(prepared, str):
        return prepared
    command, dm_file, receipt = prepared
    return _spawn_delivery(command, label, dm_file=dm_file, receipt=receipt, task_id=task_id, agent=agent)


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
    profile_home: Path | None = None, author: Optional[dict] = None,
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
        return _err(f"Delivery authority refused: {type(exc).__name__}: {exc}")
    origin = str(origin_session_id or getattr(agent, "session_id", "") or "")
    if not origin:
        return _err("Delivery requires origin_session_id; no fallback session is available.")
    dm_file = _write_dm_file(content)
    from tools import bot_delivery_queue as queue
    # The live-owner protocol has a different receipt seam. It cannot bypass the
    # fork's durable worker, effect ledger or explicit destination-session binding.
    live_eligible = (turn_identity is None and _observation is None and not _enforced
                     and not origin_session_id and not queue.enabled(Path(_agent_home(agent))))
    if profile_home is not None and live_eligible:
        try:
            record = _admit_live_dm(profile_home, dm_file, author)
        except Exception as exc:
            return json.dumps({"status": "ambiguous", "delivery_id": _dm_delivery_id(dm_file),
                "error": f"Live delivery admission could not be confirmed: {exc}. Do not resend.",
                "evidence_file": dm_file})
        if record is not None:
            command = _delivery_command(argv, dm_file, stdin_file=False, profile_home=profile_home, author=author)
            notification = json.loads(_spawn_delivery(command, label, task_id=task_id, agent=agent))
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
            return json.dumps(result)
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
    # Preserve the existing transport and completion listener, but put a
    # durable queue between acceptance and local dispatch. Peer/relay routes
    # retain their own protocol. A failed listener never deletes queued work.
    if not stdin_file:
        from tools import bot_delivery_queue as queue
        source_home = Path(_agent_home(agent))
        if queue.local_target(argv) and queue.enabled(source_home):
            try:
                store = queue.Queue(_hermes_root(source_home))
                job = store.enqueue(argv, content, receipt, source_home)
                if _idempotency is not None:
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
                return _err(f"Durable delivery could not be queued: {type(exc).__name__}: {exc}")
    if _defer_spawn:
        return command, dm_file, receipt
    return _spawn_delivery(
        command,
        label,
        dm_file=dm_file,
        receipt=receipt,
        task_id=task_id,
        agent=agent,
    )


def _spawn_delivery(
    command: str,
    label: str,
    *,
    dm_file: Optional[str] = None,
    receipt: Optional[dict[str, str]] = None,
    task_id: Optional[str],
    agent: Any,
    delivery_id: Optional[str] = None,
) -> str:
    """Launch the cleanup-owning runner and transfer file ownership on ack.

    ``dm_file`` is None for relay deliveries: the waiter command watches a
    reply file, and the envelope artifacts are owned and swept by
    ``tools/bot_relay.py`` — there is no plaintext DM tempfile to reclaim.
    """
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

        raw = terminal_tool(command, background=True, notify_on_complete=True, task_id=task_id,
                            workdir=str(Path(__file__).resolve().parent.parent), _host_local=True,
                            _completion_output_chars=REPLY_COMPLETION_CHARS)
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
    if not args or args[0] != "--run-delivery":
        return 2
    rest, author = args[1:], None
    if rest[:1] == ["--author"]:
        from agent.turn_author import parse_turn_author

        author = parse_turn_author(rest[1]) if len(rest) > 1 else None
        if author is None:
            return 2
        rest = rest[2:]
    if len(rest) < 2 or rest[0] not in ("stdin", "query-file"):
        return 2
    try:
        argv, profile_home = rest[2:], None
        if len(argv) >= 2 and argv[0] == "--profile-home":
            profile_home, argv = Path(argv[1]), argv[2:]
        return _run_delivery(argv, rest[1], stdin_file=rest[0] == "stdin", profile_home=profile_home, author=author)
    except Exception as exc:
        # Every refusal ships a typed reason on stdout so the completion notification carries it
        # back to the sender (#93091): 'target_busy' from the queue's bounded wait, otherwise the
        # same vocabulary-guarded classification the relay lane applies.
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
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(_delivery_main(sys.argv[1:]))
