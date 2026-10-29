"""Bot Chat cron delivery: deliver='bot-chat[:<profile>]' injects job output
into a local profile's canonical Bot Chat session as a real inbound turn.

Covers token parsing, target resolution (own profile / named / missing),
preflight exemption, create-time validation, the canonical admission lane,
and the delivery-targets listing used by UI pickers.
"""

from unittest import mock

import pytest

from cron import scheduler as sched
from cron import scheduler_delivery as sched_delivery
from cron.scheduler import _resolve_delivery_targets
from cron.scheduler_delivery import (
    BOT_CHAT_PLATFORM, BOT_CHAT_NOTIFY_PLATFORM,
    _deliver_to_bot_chat,
    _resolve_bot_chat_target,
    parse_bot_chat_deliver_token,
    parse_bot_chat_notify_deliver_token,
)
from cron.scheduler_preflight import _preflight_check_delivery


def test_notify_delivery_persists_in_canonical_chat_without_agent_turn(
    tmp_path, monkeypatch
):
    """bot-chat-notify is a durable, passive inbox write, not a model turn."""
    from hermes_cli import profiles
    from hermes_state import SessionDB

    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "applauseinbox"
    profile_home.mkdir(parents=True)
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("canonical", "cli")
    assert db.set_session_title("canonical", "Bot Chat")
    db.close()

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)

    def model_turn_forbidden(*_args, **_kwargs):
        raise AssertionError("passive delivery must not spawn a Hermes agent turn")

    monkeypatch.setattr(sched.subprocess, "run", model_turn_forbidden)

    def gateway_config_forbidden():
        raise AssertionError("machine-local passive delivery needs no gateway")

    monkeypatch.setattr(
        "gateway.config.load_gateway_config", gateway_config_forbidden
    )
    job = {
        "id": "slack-watch",
        "name": "Slack Watch — Applause",
        "deliver": "bot-chat-notify:applauseinbox",
        "fire_claim": {"at": "2026-08-31T19:15:55-03:00", "by": "mini"},
    }
    error = sched._deliver_result(job, "Uma atualização material no Slack.")
    replay_error = sched._deliver_result(
        job, "Uma atualização material no Slack."
    )

    assert error is None
    assert replay_error is None
    db = SessionDB(db_path=profile_home / "state.db")
    messages = db.get_messages_as_conversation("canonical")
    db.close()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["display_kind"] == "internal_notification"
    assert "[Cron delivery: Slack Watch — Applause]" in messages[0]["content"]
    assert "Uma atualização material no Slack." in messages[0]["content"]


def test_notify_target_is_validated_and_needs_no_gateway_credentials():
    """The passive token is a machine-local target on create and fire."""
    from tools.cronjob_tools import _validate_bot_chat_deliver

    assert parse_bot_chat_notify_deliver_token("bot-chat-notify") == ""
    assert (
        parse_bot_chat_notify_deliver_token("BOT-CHAT-NOTIFY:ApplauseInbox")
        == "ApplauseInbox"
    )
    with mock.patch("hermes_cli.profiles.profile_exists", return_value=True):
        assert (
            _validate_bot_chat_deliver("bot-chat-notify:applauseinbox") is None
        )
    with mock.patch("hermes_cli.profiles.profile_exists", return_value=False):
        assert _validate_bot_chat_deliver("bot-chat-notify:ghost") is not None
    assert (
        _preflight_check_delivery(
            {"id": "j1", "deliver": "bot-chat-notify:applauseinbox"}
        )
        is None
    )


# ── token parsing ────────────────────────────────────────────────────────────

def test_bare_token_targets_own_profile():
    assert parse_bot_chat_deliver_token("bot-chat") == ""
    assert parse_bot_chat_deliver_token("  Bot-Chat  ") == ""


def test_named_token_returns_profile():
    assert parse_bot_chat_deliver_token("bot-chat:research") == "research"
    assert parse_bot_chat_deliver_token("BOT-CHAT:Research") == "Research"


def test_non_bot_chat_tokens_pass_through():
    assert parse_bot_chat_deliver_token("telegram:-100:17") is None
    assert parse_bot_chat_deliver_token("origin") is None
    assert parse_bot_chat_deliver_token("local") is None
    assert parse_bot_chat_deliver_token("all") is None
    # A platform whose name merely CONTAINS bot-chat must not match.
    assert parse_bot_chat_deliver_token("bot-chatter") is None


# ── target resolution ────────────────────────────────────────────────────────





def test_unknown_profile_resolves_to_none():
    with mock.patch("hermes_cli.profiles.profile_exists", return_value=False):
        assert _resolve_bot_chat_target({"id": "j1"}, "ghost") is None


def test_resolve_delivery_targets_combines_with_platform_targets():
    """bot-chat rides the same comma-separated deliver string as platforms."""
    job = {"id": "j1", "deliver": "bot-chat,telegram"}
    with mock.patch.object(sched_delivery, "_get_home_target_chat_id", return_value="-100123"), \
         mock.patch.object(sched_delivery, "_get_home_target_thread_id", return_value=None), \
         mock.patch.object(sched_delivery, "_is_known_delivery_platform", return_value=True), \
         mock.patch.object(sched_delivery, "_resolve_origin", return_value=None):
        targets = _resolve_delivery_targets(job)
    platforms = {t["platform"] for t in targets}
    assert BOT_CHAT_PLATFORM in platforms
    assert "telegram" in platforms


# ── preflight ────────────────────────────────────────────────────────────────

def test_preflight_ignores_bot_chat_targets():
    """bot-chat needs no gateway credentials — preflight must not block it."""
    assert _preflight_check_delivery({"id": "j1", "deliver": "bot-chat"}) is None
    assert _preflight_check_delivery({"id": "j1", "deliver": "bot-chat:research"}) is None


def test_preflight_still_blocks_unknown_platforms():
    with mock.patch.object(sched_delivery, "_is_known_delivery_platform", return_value=False):
        err = _preflight_check_delivery({"id": "j1", "deliver": "nonexistent-platform"})
    assert err is not None and "not a known" in err


# ── create-time validation ───────────────────────────────────────────────────

def test_create_validation_rejects_unknown_profile():
    from tools.cronjob_tools import _validate_bot_chat_deliver

    with mock.patch("hermes_cli.profiles.profile_exists", return_value=False):
        err = _validate_bot_chat_deliver("bot-chat:ghost")
    assert err is not None


def test_create_validation_accepts_bare_and_existing():
    from tools.cronjob_tools import _validate_bot_chat_deliver

    assert _validate_bot_chat_deliver("bot-chat") is None
    assert _validate_bot_chat_deliver(None) is None
    assert _validate_bot_chat_deliver("telegram:-100") is None
    with mock.patch("hermes_cli.profiles.profile_exists", return_value=True):
        assert _validate_bot_chat_deliver("bot-chat:research") is None


# ── delivery lane ────────────────────────────────────────────────────────────

def test_deliver_message_carries_cron_attribution(tmp_path, monkeypatch):
    """The admitted turn self-identifies as scheduled output, not the user."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    captured = {}

    def fake_deliver(home, owner, message, *, delivery_id, notification_category="result"):
        captured["message"] = message
        return {"status": "settled", "message": message, "delivery_id": delivery_id}

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: {"session_id": "local-bot"})
    monkeypatch.setattr(mailbox, "deliver_to_live_owner", fake_deliver)
    assert _deliver_to_bot_chat({"id": "j1", "name": "Daily digest", "execution_id": "r1"}, "the payload", "") is None
    assert 'Cronjob "Daily digest" output' in captured["message"]
    assert "not the user" in captured["message"]
    assert "the payload" in captured["message"]


def test_failure_notice_reaches_the_owner_as_a_diagnostic(tmp_path, monkeypatch):
    """A delivered ``for_failure`` notice keeps ``notification_category="diagnostic"`` through
    the owner transport (R1/R2 minor: the category was accepted but dropped)."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = []

    def fake_deliver(home, owner, message, *, delivery_id, notification_category="result"):
        seen.append(notification_category)
        return {"status": "settled", "message": message, "delivery_id": delivery_id,
                "notification_category": notification_category}

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: {"session_id": "local-bot"})
    monkeypatch.setattr(mailbox, "deliver_to_live_owner", fake_deliver)
    monkeypatch.setattr("gateway.warning_notifications.warning_notifications_enabled", lambda *a, **k: True)
    job = {"id": "j1", "name": "n", "execution_id": "r1"}
    assert _deliver_to_bot_chat(job, "boom", "", for_failure=True) is None
    assert _deliver_to_bot_chat(dict(job, execution_id="r2"), "ok", "") is None
    assert seen == ["diagnostic", "result"]


def test_deliver_without_authority_is_parked_not_a_second_writer(tmp_path, monkeypatch):
    """No running authority → parked for a later tick (explicit queued status); never a local CLI turn."""
    import subprocess
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", mock.Mock(side_effect=AssertionError("no local inference fallback")))
    monkeypatch.setattr(mailbox, "find_canonical_live_owner",
                        mock.Mock(side_effect=ValueError("profile authority is not ready")))
    err = _deliver_to_bot_chat({"id": "j1", "name": "n", "execution_id": "r1"}, "out", "")
    assert err is not None and "queued" in err and "not running" in err


def test_failure_notice_to_a_profile_hiding_warnings_is_suppressed_not_sent(tmp_path, monkeypatch):
    """A ``for_failure`` notice whose TARGET profile hides warning notifications is booked as a
    durable ``suppressed`` disposition (flagged on the job), never admitted to the owner; a
    requested (non-failure) result is never gated."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("display: {suppress_warning_notifications: true}\n")
    admitted = []

    def fake_deliver(home, owner, message, *, delivery_id, notification_category="result"):
        admitted.append(message)
        return {"status": "queued", "message": message, "delivery_id": delivery_id}

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: {"session_id": "bot"})
    monkeypatch.setattr(mailbox, "deliver_to_live_owner", fake_deliver)

    failure = {"id": "j1", "name": "n", "execution_id": "r1"}
    assert _deliver_to_bot_chat(failure, "diagnostic", "", for_failure=True) is None
    assert failure["_notification_all_targets_suppressed"] is True
    assert failure["_bot_chat_delivery_receipts"]["bot-chat:(own)"]["status"] == "suppressed"
    assert admitted == []

    result = {"id": "j1", "name": "n", "execution_id": "r2"}
    outcome = _deliver_to_bot_chat(result, "the report", "")
    assert outcome and "queued" in outcome
    assert not result.get("_notification_all_targets_suppressed")
    assert len(admitted) == 1


# ── delivery-targets listing (UI pickers) ────────────────────────────────────

def test_delivery_targets_include_local_profiles():
    with mock.patch("hermes_cli.profiles.list_profile_names",
                    return_value=["default", "research"]):
        targets = sched_delivery.cron_delivery_targets()
    ids = [t["id"] for t in targets]
    assert f"{BOT_CHAT_PLATFORM}:default" in ids
    assert f"{BOT_CHAT_PLATFORM}:research" in ids
    assert f"{BOT_CHAT_NOTIFY_PLATFORM}:default" in ids
    assert f"{BOT_CHAT_NOTIFY_PLATFORM}:research" in ids
    bot_chat_entries = [t for t in targets if t["id"].startswith(BOT_CHAT_PLATFORM)]
    # No gateway home channel needed for bot-chat targets.
    assert all(t["home_target_set"] for t in bot_chat_entries)


# ── legacy cron/bot_chat_pending upgrade path ────────────────────────────────

def test_tick_hands_legacy_pending_records_to_the_live_owner_once(tmp_path, monkeypatch):
    """Records the retired CLI lane left in cron/bot_chat_pending are never orphaned: a queued
    (never-started) record is admitted to the live owner under its own id; a claimed one (an
    uncertain CLI turn) is never resent; the files stay as evidence."""
    import json

    import cron.jobs as J
    import cron.scheduler as S
    from cron import scheduler_authority
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(J, "HERMES_DIR", tmp_path)
    monkeypatch.setattr(J, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(S, "_hermes_home", tmp_path)
    monkeypatch.setattr(S, "_sweep_mcp_orphans", lambda: None)
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", lambda *, allow_connect=True: None)
    (tmp_path / "state.db").write_text("")
    root = tmp_path / "cron" / "bot_chat_pending"
    root.mkdir(parents=True)
    queued, claimed = "a" * 64, "b" * 64
    for key, status in ((queued, "queued"), (claimed, "claimed")):
        (root / f"{key}.json").write_text(json.dumps(dict(
            id=key, status=status, job={"id": "j1", "name": "Digest"}, content="the report",
            profile="", home=str(tmp_path), sequence=1, for_failure=True)))
    admitted = []

    def fake_deliver(home, owner, message, *, delivery_id, notification_category="result"):
        admitted.append((delivery_id, message, notification_category))
        return {"status": "queued", "message": message, "delivery_id": delivery_id}

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: {"session_id": "bot"})
    monkeypatch.setattr(mailbox, "deliver_to_live_owner", fake_deliver)

    S.tick(verbose=False)
    S.tick(verbose=False)

    assert [(key, category) for key, _, category in admitted] == [(queued, "diagnostic")]
    assert 'Cronjob "Digest" output' in admitted[0][1] and "the report" in admitted[0][1]
    assert json.loads((root / f"{queued}.json").read_text())["status"] == "transferred"
    assert json.loads((root / f"{claimed}.json").read_text())["status"] == "claimed"


@pytest.mark.parametrize("targets", ["bot-chat-notify:inbox,telegram:123", "telegram:123,bot-chat-notify:inbox"])
def test_mixed_notify_persists_once_before_external_config_failure(tmp_path, monkeypatch, targets):
    from hermes_cli import profiles
    from hermes_state import SessionDB

    root = tmp_path / ".hermes"
    home = root / "profiles" / "inbox"
    home.mkdir(parents=True)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("canonical", "cli")
        assert db.set_session_title("canonical", "Bot Chat")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    attempts = []

    def unavailable_config():
        with SessionDB(db_path=home / "state.db") as db:
            messages = db.get_messages("canonical")
            assert len(messages) == 1, "local notice must precede connector config"
        attempts.append(True)
        raise RuntimeError("external connector configuration unavailable")

    monkeypatch.setattr("gateway.config.load_gateway_config", unavailable_config)
    job = {"id": "mixed-watch", "name": "Mixed Watch", "deliver": targets,
           "fire_claim": {"at": "2026-09-27T22:00:00Z", "by": "mini"}}
    for _ in range(2):
        error = sched._deliver_result(job, "one local notification")
        assert "external connector configuration unavailable" in error
    assert len(attempts) == 2
    with SessionDB(db_path=home / "state.db") as db:
        rows = db.get_messages("canonical")
    assert len(rows) == 1
    assert rows[0]["display_kind"] == "internal_notification"
