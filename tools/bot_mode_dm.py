"""Bot Mode agent-to-agent DM tool — ``message_agent``.

A structured, Bot-Chat-only tool that lets a Bot Mode agent message a
teammate agent (another Hermes profile on this install, or an agent on a
registered peer gateway) WITHOUT hand-assembling shell commands.

Why this exists (Aug 2026): the Bot Mode teammate protocol taught agents to
DM each other via a prompt-injected ``hermes -p <bot> chat ...`` shellout.
That transport works, but the *invocation* was fragile — quoting traps
(#91339/#91304), temp-file choreography, dead-profile races — and the
Desktop's remote-mention path forwarded raw user text verbatim (#91397).
``message_agent`` replaces the invocation with a real tool call: the message
is a parameter, the target is validated against the live roster, the
attribution prefix is applied server-side, and the reply arrives through the
existing background-process notification path (fire-and-forget, never
blocks the sender's turn).

Containment contract (MUST hold — reviewers check all three):
- The tool schema is injected ONLY into a bot's canonical "Bot Chat"
  session on Bot-Mode-managed installs — the exact same gate as the
  protocol section in ``tools/bot_mode_probe.py``. It is NOT registered in
  the global tool registry, is NOT part of any toolset, and never appears
  in CLI sessions, ordinary gateway chats, group-room member sessions
  (titled "Group: …"), cron agents, or subagents.
- Dispatch is title-gated again at execution time (defense in depth): a
  forged call from a session that shouldn't have the tool returns a
  structured error instead of delivering.
- Everything here is additive. The legacy protocol transports
  (``hermes -p`` / ``hermes peer dm``) keep working for older prompts.

The transports themselves are unchanged and proven:
- local teammate  → ``hermes -p <name> chat --in ~ -c "Bot Chat"
  --create-if-missing -Q --query-file <tmp>`` (one turn, reply on stdout)
- peer teammate   → ``hermes peer dm <peer>[/<name>] < <tmp>``

Both run through ``terminal_tool(background=True, notify_on_complete=True)``
so the reply lands as a completion notification on the sender's NEXT turn —
the same wake shape every Bot Mode agent already knows.
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

logger = logging.getLogger(__name__)

MESSAGE_AGENT_TOOL_NAME = "message_agent"

# Message body cap — generous for real work products, small enough that a
# runaway paste can't turn one DM into a context bomb on the recipient.
MESSAGE_MAX_CHARS = 16000

# A runner normally owns and removes each file. This bounds the residual
# plaintext lifetime if the machine dies after background-spawn acknowledgement
# but before the runner reaches its ``finally`` block.
_DM_DIR_NAME = "hermes-dm"
_DM_STALE_SECONDS = 24 * 60 * 60

_PEER_TARGET_RE = re.compile(r"^([a-z0-9][a-z0-9_-]{0,63})/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})$")
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
                "delivery acknowledgement. It does NOT return their reply and you must "
                "not wait or poll for one — send it, finish your turn, and the reply "
                "arrives later as a background-process completion notification that "
                "wakes you. COMPOSE the message yourself: write what YOU want to say to "
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


def ensure_message_agent_tool(agent: Any) -> bool:
    """Inject the ``message_agent`` schema into a Bot Chat agent's tool list.

    Called once per turn from the conversation loop. Idempotent and
    deterministic for the life of a session: the gate (canonical Bot Chat
    title on a Bot-Mode-managed install) is stable from the session's first
    turn, so the tool list is byte-identical across turns — prompt-cache
    safe. Every non-Bot-Chat session fails the gate on every turn and never
    sees the schema. Never raises.
    """
    try:
        if not getattr(agent, "_bot_mode_protocol", True):
            return False
        tools = getattr(agent, "tools", None)
        if tools:
            for tool in tools:
                if (
                    isinstance(tool, dict)
                    and tool.get("function", {}).get("name") == MESSAGE_AGENT_TOOL_NAME
                ):
                    return True
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        if _session_title(agent) != BOT_CHAT_TITLE:
            return False
        # Managed-install check, NOT section non-emptiness: a profile whose
        # SOUL.md carries the legacy plugin-appended protocol text gets an
        # empty section (dedupe) but must still receive the tool — otherwise
        # upgraded installs silently lose A2A messaging (Aug 2026).
        if not is_bot_mode_managed(_agent_home(agent)):
            return False
        if agent.tools is None:
            agent.tools = []
        agent.tools.append(message_agent_tool_schema())
        valid = getattr(agent, "valid_tool_names", None)
        if isinstance(valid, set):
            valid.add(MESSAGE_AGENT_TOOL_NAME)
        return True
    except Exception:  # pragma: no cover — must never break a turn
        logger.debug("ensure_message_agent_tool failed", exc_info=True)
        return False


# ── roster resolution ────────────────────────────────────────────────────────


def _hermes_root(home: Path) -> Path:
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _self_profile_name(home: Path) -> str:
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _local_roster(root: Path) -> list[str]:
    """Profile names on this install: default + every named profile."""
    names = ["default"]
    try:
        profiles = root / "profiles"
        if profiles.is_dir():
            for child in sorted(profiles.iterdir()):
                if child.is_dir():
                    names.append(child.name)
    except Exception:
        pass
    return names


def _peers(root: Path) -> list[str]:
    try:
        from tools.bot_mode_probe import _peers as _probe_peers

        return _probe_peers(root)
    except Exception:
        return []


def _handle(name: str) -> str:
    return "hermes" if name == "default" else name


def _resolve_local_name(target: str, roster: list[str]) -> Optional[str]:
    """Map a target handle to a profile name ('hermes' → 'default')."""
    want = target.strip()
    if not want:
        return None
    if want.lower() == "hermes":
        return "default" if "default" in roster else None
    for name in roster:
        if name.lower() == want.lower():
            return name
    return None


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

    ``agent`` is the calling AIAgent (threaded by the executor) — used for
    the Bot Chat gate, the sender identity, and the session key so the
    spawned transport is tracked against the right session.
    """
    # ── defense-in-depth gate: only a canonical Bot Chat may deliver ──
    home = _agent_home(agent)
    try:
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        title = _session_title(agent)
        if title != BOT_CHAT_TITLE:
            return _err(
                "message_agent is only available in a Bot Mode 'Bot Chat' session. "
                "This session is not one; do not retry."
            )
        if not is_bot_mode_managed(home):
            return _err(
                "This install is not Bot-Mode-managed (no bot roster); "
                "message_agent is unavailable. Do not retry."
            )
    except Exception as exc:  # pragma: no cover — defensive
        return _err(f"Bot Mode gate check failed: {exc}")

    root = _hermes_root(Path(home))
    me = _self_profile_name(Path(home))
    roster = _local_roster(root)
    peers = _peers(root)
    teammates = [_handle(n) for n in roster if n != me]

    body = str(message or "").strip()
    if not body:
        return _err("message is required — compose what you want to say to that agent.")
    if len(body) > MESSAGE_MAX_CHARS:
        return _err(
            f"message too long ({len(body)} chars > {MESSAGE_MAX_CHARS}). "
            "Send the essentials; share large content as a file path instead."
        )

    raw_target = str(target or "").strip().lstrip("@")
    if not raw_target:
        return _err("target is required.", roster=teammates, peers=peers)

    hermes_cli: Optional[str] = None

    def resolved_hermes_cli() -> str:
        nonlocal hermes_cli
        if hermes_cli is None:
            from tools.bot_relay import _hermes_cli

            hermes_cli = _hermes_cli()
        return hermes_cli

    sender_handle = _handle(me)
    prefix = f"Message from 🤖 {sender_handle} (@{sender_handle}): "

    # ── peer target: '<peer>/<agent>' or a bare registered peer name ──
    peer_match = _PEER_TARGET_RE.match(raw_target)
    bare_peer = raw_target.lower() if raw_target.lower() in peers else None
    if peer_match or bare_peer:
        peer_name = peer_match.group(1) if peer_match else bare_peer
        peer_profile = peer_match.group(2) if peer_match else None
        if peer_name not in peers:
            return _err(
                f"No registered peer named '{peer_name}'.", roster=teammates, peers=peers
            )
        dm_target = f"{peer_name}/{peer_profile}" if peer_profile else peer_name
        label = f"@{peer_profile or peer_name} on peer '{peer_name}'"
        return _start_delivery(
            [resolved_hermes_cli(), "peer", "dm", dm_target],
            prefix + body,
            label,
            stdin_file=True,
            task_id=task_id,
            agent=agent,
            origin_session_id=origin_session_id,
        )

    # ── local teammate ──
    if not _LOCAL_TARGET_RE.match(raw_target) and "@" not in raw_target:
        return _err(f"Invalid target: {raw_target!r}.", roster=teammates, peers=peers)
    resolved = _resolve_local_name(raw_target, roster) if _LOCAL_TARGET_RE.match(raw_target) else None
    if resolved is None:
        # ── cross-connection teammate (Desktop relay) ──
        # Every gateway connected to the user's Desktop is reachable: the
        # relay roster lists agents on the other connections; delivery rides
        # the Desktop's own persistent socket to that gateway.
        relay_refusal = _relay_enforce_refusal()
        if relay_refusal is not None:
            return relay_refusal
        relayed = _try_relay_delivery(
            root, raw_target, body, me, sender_handle, task_id=task_id, agent=agent
        )
        if relayed is not None:
            return relayed
        return _err(
            f"No teammate named '{raw_target}' on this install, on a connected "
            "machine, or on a registered peer. Pick a name from the roster "
            "(roles are listed in your system prompt).",
            roster=teammates,
            peers=peers,
        )
    if resolved == me:
        # Same-name target on ANOTHER connection (e.g. this gateway's
        # 'default' messaging the cloud 'default') — try the relay before
        # calling it a self-message.
        relay_refusal = _relay_enforce_refusal()
        if relay_refusal is not None:
            return relay_refusal
        relayed = _try_relay_delivery(
            root, raw_target, body, me, sender_handle, task_id=task_id, agent=agent
        )
        if relayed is not None:
            return relayed
        return _err("You can't message yourself. Pick a teammate from the roster.")

    exact_session, route_reason = _resolve_target_bot_session(
        resolved, origin_session_id
    )
    if origin_session_id and not exact_session:
        return _err(
            f"Delivery origin session {origin_session_id!r} is not an eligible Bot Chat "
            f"for profile '{resolved}' ({route_reason}); refusing title fallback."
        )
    route_argv = [resolved_hermes_cli(), "-p", resolved]
    if exact_session:
        # Top-level --resume reaches the exact session (or its compression
        # continuation), before the chat subcommand can inspect a title.
        route_argv += ["--resume", exact_session]
    route_argv += [
        "chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q"
    ]

    return _start_delivery(
        route_argv,
        prefix + body,
        f"@{_handle(resolved)}",
        stdin_file=False,
        task_id=task_id,
        agent=agent,
        origin_session_id=origin_session_id,
        route_reason=route_reason,
    )


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


def _try_relay_delivery(
    root: Path,
    raw_target: str,
    body: str,
    me: str,
    sender_handle: str,
    *,
    task_id: Optional[str],
    agent: Any,
) -> Optional[str]:
    """Cross-connection delivery via the Desktop relay, or None if the
    target doesn't resolve against the relay roster.

    The envelope is queued on disk; the Desktop drains it over RPC and
    delivers on the target connection's own socket. A background waiter is
    spawned immediately so the relayed reply wakes the sender through the
    standard completion-notification path — identical UX to a local DM.
    """
    try:
        from tools.bot_relay import (
            EnvelopeRefusedError,
            enqueue_envelope,
            read_remote_roster,
            resolve_remote_target,
            waiter_command,
        )

        roster = read_remote_roster(root)
        if not roster:
            return None
        match = resolve_remote_target(raw_target, roster)
        if match is None:
            return None
        if match == "ambiguous":
            forms = ", ".join(
                f"{r['handle']}@{r['connection_id']}"
                for r in roster
                if r["handle"].lower() == raw_target.strip().lstrip("@").lower()
            )
            return _err(
                f"'{raw_target}' exists on several connected machines — "
                f"disambiguate with one of: {forms}."
            )
        from agent.durable_admission import reserve_observed_effect, observation_gap
        relay_observation = reserve_observed_effect(
            "relay:" + str(match["connection_id"]) + ":" + str(match["handle"]), body,
        )
        if relay_observation is not None:
            # Desktop owns a different ACK seam. This adapter cannot claim it.
            observation_gap(relay_observation, "ack_missing")
        try:
            envelope = enqueue_envelope(
                root,
                target=match,
                message=f"Message from 🤖 {sender_handle} (@{sender_handle}): {body}",
                sender_profile=me,
                sender_handle=sender_handle,
            )
        except EnvelopeRefusedError as exc:
            # Fail fast: target definitively offline — nothing was queued.
            # Structured refusal so the agent can distinguish it from a
            # resolution error ('runtime_offline' per the #93091 reason enum).
            return json.dumps({"error": str(exc), "reason": exc.reason})
        label = f"@{match['handle']} on {match['connection_label'] or match['connection_id']}"
        return _spawn_delivery(
            waiter_command(root, envelope), label, task_id=task_id, agent=agent
        )
    except Exception:
        logger.debug("relay delivery attempt failed", exc_info=True)
        return None


def _dm_dir() -> Path:
    uid_getter = getattr(os, "getuid", None)
    uid = uid_getter() if callable(uid_getter) else None
    dirname = f"{_DM_DIR_NAME}-{uid}" if uid is not None else _DM_DIR_NAME
    path = Path(tempfile.gettempdir()) / dirname
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


def cleanup_bot_dm_cache(
    max_age_hours: float = _DM_STALE_SECONDS / 3600, *, now: float | None = None
) -> int:
    """Delete orphaned DM payload files older than *max_age_hours*.

    Same contract as the other ``cleanup_*_cache`` helpers — returns the
    number of files removed — so the gateway housekeeping loop can prune
    this cache on the same hourly cadence as the media caches, even on
    installs that never send another DM (the in-band sweep in
    ``_write_dm_file`` only runs when a DM is written).
    """
    cutoff = (time.time() if now is None else now) - max_age_hours * 3600
    removed = 0
    # Include the legacy temp-root locations so upgrades clean files created
    # by versions predating the dedicated directory.
    temp_root = Path(tempfile.gettempdir())
    locations: list[tuple[Path, str]] = [
        (temp_root, "hermes-dm-*.txt"),
        (temp_root, "hermes-relay-dm-*.txt"),
    ]
    try:
        locations.append((_dm_dir(), "*.txt"))
    except OSError:
        pass
    for directory, pattern in locations:
        try:
            for candidate in directory.glob(pattern):
                try:
                    if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                        candidate.unlink()
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
    return removed


def _sweep_stale_dm_files(*, now: float | None = None) -> None:
    """Best-effort cleanup for files orphaned before their runner started."""
    cleanup_bot_dm_cache(now=now)


def _write_dm_file(content: str) -> str:
    """The message rides a temp file — never inline shell text."""
    _sweep_stale_dm_files()
    fd, path = tempfile.mkstemp(prefix="dm-", suffix=".txt", dir=_dm_dir(), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        # fdopen owns the descriptor once it succeeds, but if fdopen itself
        # failed the raw descriptor is still ours. Closing twice is harmless.
        try:
            os.close(fd)
        except OSError:
            pass
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


def _unlink_dm_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _delivery_lock(argv: list[str], *, stdin_file: bool):
    """Per-profile turn lock context for a LOCAL teammate delivery (#93091).

    Local deliveries (``hermes -p <profile> chat …``) collide with relay
    deliveries into the same profile — both run a Bot Chat turn on this
    install — so the turn window is serialized on the shared cross-process
    lock in ``tools.bot_relay``. Peer transports (stdin mode) run on the
    remote gateway; their turn is locked THERE by its own deliver path.
    """
    # The CLI element is matched by basename: local_delivery_command now
    # resolves the venv-relative hermes next to this gateway's interpreter
    # (#93590 — service contexts lack PATH), so argv[0] may be an absolute
    # path (and on Windows carries the .exe suffix). Split on both
    # separators so the shape matches regardless of which platform built
    # the argv.
    cli = (argv[0] if argv else "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if (
        stdin_file
        or len(argv) < 3
        or cli not in ("hermes", "hermes.exe")
        or argv[1] != "-p"
    ):
        return contextlib.nullcontext()
    from tools.bot_relay import acquire_turn_lock

    home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    return acquire_turn_lock(_hermes_root(home), argv[2])


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


def _run_delivery(argv: list[str], dm_file: str, *, stdin_file: bool, lock_held: bool = False, strict_authority: bool = False) -> int:
    """Run one DM transport and remove its plaintext file after consumption.

    The turn execution window (not the enqueue) holds the target profile's
    cross-process lock, so two deliveries into one profile queue instead of
    racing; a bounded wait ends in a structured 'target_busy' refusal.

    Local (query-file) turns get one policy-gated retry (#93091 item 5):
    transient failures re-run the same session; a context_overflow re-run
    lets the retried turn's pre-API compaction pass compact the Bot Chat
    transcript first (agent/conversation_loop.py) — the sanctioned
    compression lever; no fresh session is ever minted. Auth/quota/config
    failures never retry. Peer transports (stdin mode) retry on their own
    gateway's deliver path, not here.
    """
    returncode = 1
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
                        env=_delivery_runtime_env(),
                    )
                returncode = proc.returncode
            else:
                proc = subprocess.run(
                    [*transport_argv, "--query-file", dm_file],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=_delivery_runtime_env(),
                )
                returncode = proc.returncode
            if proc.returncode != 0 and not stdin_file:
                from tools.bot_failure_reasons import (
                    RETRY_NONE,
                    classify_agent_error,
                    retry_action,
                )

                detail = (proc.stderr or proc.stdout or "").strip()[-500:]
                if retry_action(classify_agent_error(detail)) != RETRY_NONE:
                    proc = subprocess.run(
                        [*transport_argv, "--query-file", dm_file],
                        check=False,
                        capture_output=True,
                        text=True,
                        env=_delivery_runtime_env(),
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
        _unlink_dm_file(dm_file)
        _unlink_dm_file(str(_delivery_envelope_path(dm_file)))


def _delivery_command(argv: list[str], dm_file: str, *, stdin_file: bool) -> str:
    """Build an argv-safe command for the cleanup-owning background runner."""
    runner_argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--run-delivery",
        "stdin" if stdin_file else "query-file",
        dm_file,
        *argv,
    ]
    if sys.platform == "win32":
        # The tracked local backend uses Git Bash on native Windows. Forward
        # slashes preserve native drive paths while remaining executable by
        # that shell; backslash-form paths are parsed as command names and die
        # with exit 127 before this runner starts.
        runner_argv = [part.replace("\\", "/") for part in runner_argv]
    return shlex.join(runner_argv)


def _start_delivery(
    argv: list[str], content: str, label: str, *, stdin_file: bool,
    task_id: Optional[str], agent: Any, origin_session_id: Optional[str] = None,
    route_reason: str = "canonical_title_fallback",
) -> str:
    from agent import durable_admission as da

    if da.admission_enabled():
        return _start_enforced_delivery(argv, content, label, stdin_file=stdin_file, task_id=task_id,
            agent=agent, origin_session_id=origin_session_id, route_reason=route_reason)
    if not da.observation_enabled():
        return _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=origin_session_id, route_reason=route_reason)
    observation = da.reserve_observed_effect(label, content)
    if observation is None:
        return _start_delivery_inner(argv, content, label, stdin_file=stdin_file,
            task_id=task_id, agent=agent, origin_session_id=origin_session_id, route_reason=route_reason)
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
            route_reason=route_reason, _observation=observation, _identity=identity,
            _fence=fence, _idempotency=key, _defer_spawn=True)
    if isinstance(prepared, str):
        return prepared
    command, dm_file, receipt = prepared
    return _spawn_delivery(command, label, dm_file=dm_file, receipt=receipt, task_id=task_id, agent=agent)


def _start_enforced_delivery(argv, content, label, *, stdin_file, task_id, agent,
                             origin_session_id, route_reason):
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
                task_id=task_id, agent=agent, origin_session_id=origin, route_reason=route_reason,
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
    dm_file = _write_dm_file(content)
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
        )
        if receipt.get("duplicate") and receipt.get("state") == "delivered":
            _unlink_dm_file(dm_file)
            _unlink_dm_file(dm_file + ".receipt.json")
            _unlink_dm_file(str(_delivery_envelope_path(dm_file)))
            return json.dumps({"status": "delivered", "to": label,
                               "delivery": _public_delivery_receipt(receipt),
                               "detail": "Delivery already persisted for this exact request; no duplicate spawn."})
        command = _delivery_command(argv, dm_file, stdin_file=stdin_file)
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
                           "delivery": _public_delivery_receipt(receipt),
                           "detail": "Request saved; destination has not received it yet. " + detail})
    try:
        from tools.terminal_tool import terminal_tool

        raw = terminal_tool(
            command,
            background=True,
            notify_on_complete=True,
            task_id=task_id,
            workdir=str(Path(__file__).resolve().parent.parent),
            _host_local=True,
        )
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
        if not proc_id:
            if queued:
                return queued_without_listener("Completion listener unavailable; inspect the updates inbox.")
            _release_unspawned_delivery(dm_file)
            return _err(f"Delivery to {label} failed to start: no process id returned")
        # From this point the background runner owns the file and removes it
        # only after the local query-file or peer stdin consumer has finished.
        transferred = True
        return json.dumps(
            {
                "status": receipt.get("state", "queued") if queued else "accepted",
                "to": label,
                "detail": ("Request durably queued; NOT delivered or executing yet. "
                           "Do not resend or poll. Finish this turn; its completion notification will carry the reply. "
                           "Durable status is also available in the updates inbox.") if queued else (
                    f"Message dispatched to {label}. This is asynchronous — do NOT wait "
                    "or poll. Finish your turn now; when the delivery completes, its "
                    "notification carries the reply — relay it then, attributed to "
                    "that agent."
                ),
                **({"process_id": proc_id} if proc_id else {}),
                **({"delivery": _public_delivery_receipt(receipt)} if receipt else {}),
                "sent_at": int(time.time()),
            }
        )
    except Exception as exc:
        if queued:
            return queued_without_listener("Completion listener unavailable; inspect the updates inbox.")
        _release_unspawned_delivery(dm_file)
        logger.error("message_agent delivery spawn failed: %s", exc, exc_info=True)
        return _err(f"Delivery to {label} could not be started: {exc}")
    finally:
        if dm_file and not transferred:
            _unlink_dm_file(dm_file)


def _delivery_main(args: list[str]) -> int:
    if len(args) < 3 or args[0] != "--run-delivery":
        return 2
    stdin_file = args[1] == "stdin"
    if not stdin_file and args[1] != "query-file":
        return 2
    dm_file = args[2]
    try:
        return _run_delivery(args[3:], dm_file, stdin_file=stdin_file)
    except Exception as exc:
        # 'target_busy' extends the #93091 item-1 structured refusal enum:
        # the queued delivery gave up after its bounded wait — surface the
        # structured payload on stdout so the completion notification carries
        # it back to the sending agent.
        reason = getattr(exc, "reason", "")
        if reason == "target_busy":
            print(json.dumps({"error": str(exc), "reason": "target_busy"}))
            return 1
        print(
            f"message_agent delivery failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


# ── agent-context helpers (mirror system_prompt.py's resolution) ─────────────


def _agent_home(agent: Any) -> str:
    """The calling agent's OWN home (session-db derived), not ambient env."""
    try:
        sdb = getattr(agent, "_session_db", None)
        db_path = getattr(sdb, "db_path", None)
        if db_path:
            return str(Path(db_path).parent)
    except Exception:
        pass
    return os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _session_title(agent: Any) -> str:
    title = str(getattr(agent, "_session_title_hint", "") or "").strip()
    if title:
        return title
    try:
        sdb = getattr(agent, "_session_db", None)
        sid = getattr(agent, "session_id", None)
        if sdb and sid:
            return str(sdb.get_session_title(sid) or "").strip()
    except Exception:
        pass
    return ""


if __name__ == "__main__":  # pragma: no cover - exercised as a background process
    raise SystemExit(_delivery_main(sys.argv[1:]))
