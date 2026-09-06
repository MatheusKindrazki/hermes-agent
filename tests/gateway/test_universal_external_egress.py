"""Every final outbound lane crosses the shared policy/outbox seam."""

import json
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.egress_policy import EgressPolicy
from gateway.reliability_outbox import ReliabilityOutbox
from gateway.session import SessionSource, build_session_key
from tests.gateway.test_background_command import _make_runner as make_background_runner
from tests.gateway.test_delivery_ledger_producer import _Adapter, _event, _run
from tests.gateway.test_send_voice_reply_notify import (
    _fake_tts_call,
    _make_event as make_voice_event,
    _runner_with_adapter,
)
from tests.gateway.test_tts_media_routing import (
    _DiscordMediaFailureAdapter,
    _allowed_media_path,
    _event as media_event,
)


def _config(tmp_path, *, egress="enforce", outbox="shadow"):
    return {
        "gateway": {
            "reliability": {
                "egress": {"mode": egress},
                "outbox": {
                    "mode": outbox,
                    "db_path": str(tmp_path / "outbox.sqlite3"),
                    "payload_dir": str(tmp_path / "payloads"),
                },
            }
        }
    }


def _identity(version: str):
    return {
        "profile": "luguistaff",
        "tenant": "lugui",
        "machine": "personal-mac-mini",
        "work_id": "01a04a0f-455a-7bea-a064-4aa68b009d39",
        "session_id": "runtime-sid",
        "policy_version": "tone-v1",
        "version": version,
    }


def _enable_shadow(owner, tmp_path):
    config = _config(tmp_path)
    owner._egress_policy = EgressPolicy.from_config(config)
    owner._reliability_outbox = ReliabilityOutbox.from_config(
        config, hermes_home=tmp_path
    )


def _event_types(tmp_path):
    with sqlite3.connect(tmp_path / "outbox.sqlite3") as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM outbox_events ORDER BY created_at,event_id"
            ).fetchall()
        ]


@pytest.mark.asyncio
async def test_runner_auto_tts_shadow_enqueues_without_native_voice(
    monkeypatch, tmp_path, unit_tone_gate
):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _fake_tts_call(monkeypatch)
    native_voice = AsyncMock()
    runner = _runner_with_adapter(native_voice)
    runner._thread_metadata_for_source = MagicMock(
        return_value={**_identity("auto-tts-1"), "tone_envelope": unit_tone_gate()}
    )
    _enable_shadow(runner, tmp_path)

    await runner._send_voice_reply(make_voice_event(), "spoken final")

    native_voice.assert_not_awaited()
    assert _event_types(tmp_path) == ["gateway_runner_auto_tts"]


@pytest.mark.asyncio
async def test_discord_voice_channel_auto_tts_shadow_skips_native_playback(
    monkeypatch, tmp_path, unit_tone_gate
):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _fake_tts_call(monkeypatch)
    play_voice = AsyncMock()
    adapter = SimpleNamespace(
        is_in_voice_channel=lambda _guild_id: True,
        play_in_voice_channel=play_voice,
        send_voice=AsyncMock(),
    )
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._thread_metadata_for_source = MagicMock(
        return_value={**_identity("discord-vc-tts-1"), "tone_envelope": unit_tone_gate()}
    )
    _enable_shadow(runner, tmp_path)
    event = make_voice_event()
    event.source.platform = Platform.DISCORD
    event.raw_message = SimpleNamespace(guild_id=77, guild=None)

    await runner._send_voice_reply(event, "spoken in voice channel")

    play_voice.assert_not_awaited()
    assert _event_types(tmp_path) == ["gateway_runner_auto_tts"]


@pytest.mark.asyncio
async def test_background_result_shadow_enqueues_without_native_send(tmp_path, unit_tone_gate):
    runner = make_background_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], "background answer"))
    adapter.extract_images = MagicMock(return_value=([], "background answer"))
    adapter.toolsets_for_source = MagicMock(return_value=None)
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._thread_metadata_for_source = MagicMock(
        return_value={**_identity("background-1"), "tone_envelope": unit_tone_gate()}
    )
    _enable_shadow(runner, tmp_path)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="12345",
        chat_id="67890",
    )
    result = {"final_response": "background answer", "messages": []}

    with patch(
        "gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}
    ), patch("gateway.run._load_gateway_config", return_value={}), patch(
        "run_agent.AIAgent"
    ) as agent_cls:
        agent = MagicMock()
        agent.run_conversation.return_value = result
        agent_cls.return_value = agent
        await runner._run_background_task("do work", source, "bg-1")

    adapter.send.assert_not_awaited()
    assert _event_types(tmp_path) == ["gateway_background_result"]


@pytest.mark.asyncio
async def test_nonstream_final_shadow_enqueues_without_native_adapter(
    monkeypatch, tmp_path, unit_tone_gate
):
    adapter = _Adapter()
    _enable_shadow(adapter, tmp_path)
    monkeypatch.setattr(
        "gateway.platforms.base._thread_metadata_for_source",
        lambda *_args, **_kwargs: {**_identity("nonstream-1"), "tone_envelope": unit_tone_gate()},
    )

    await _run(adapter, _event(), response="nonstream final")

    assert adapter.sent == []
    assert _event_types(tmp_path) == ["gateway_adapter_final"]


@pytest.mark.asyncio
async def test_nonstream_media_policy_reject_has_no_native_failure_notice(
    monkeypatch, tmp_path
):
    adapter = _DiscordMediaFailureAdapter()
    config = _config(tmp_path, outbox="off")
    adapter._egress_policy = EgressPolicy.from_config(config)
    adapter._reliability_outbox = ReliabilityOutbox.disabled(hermes_home=tmp_path)
    media_file = _allowed_media_path(tmp_path, monkeypatch, "blocked.mp4")
    adapter._message_handler = AsyncMock(return_value=f"MEDIA:{media_file}")
    adapter.send_video = AsyncMock()
    adapter.send_document = AsyncMock()
    adapter.send_voice = AsyncMock()
    adapter.send_multiple_images = AsyncMock()
    monkeypatch.setattr(
        "gateway.platforms.base._thread_metadata_for_source",
        lambda *_args, **_kwargs: {},
    )
    event = media_event()

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send_video.assert_not_awaited()
    adapter.send_document.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()
    assert adapter.notices == []
