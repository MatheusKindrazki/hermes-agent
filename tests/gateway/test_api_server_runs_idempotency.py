"""Focused idempotency behavior contracts."""

import asyncio

import hashlib

import json

import threading

import time

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiohttp import web

from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig

from gateway.platforms.api_server_runs import _RunStream

from gateway.platforms.api_server import (
    APIServerAdapter,
    _api_request_profile,
    _approval_event_choices,
    cors_middleware,
    security_headers_middleware,
)

from gateway.run_idempotency import RunIdempotencyLedger

from tools import approval as approval_mod

from tools import approval_gateway_wait

from tests.waiting import async_wait_until


from tests.gateway.test_api_server_runs import (_claim_run, _create_runs_app, _make_adapter)

def _use_idempotency_db(adapter, path):
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

    adapter._run_idempotency_store.close()
    adapter._run_idempotency_store = RunIdempotencyStore(str(path))


class TestRunIdempotency:
    @pytest.mark.asyncio
    async def test_invalid_body_does_not_consume_idempotency_key(
        self, adapter, tmp_path
    ):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers = {"Idempotency-Key": "valid-after-rejection"}
            rejected = await cli.post(
                "/v1/runs", json={"input": ""}, headers=headers
            )
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                accepted = await cli.post(
                    "/v1/runs", json={"input": "valid"}, headers=headers
                )
        assert rejected.status == 400
        assert accepted.status == 202

    @pytest.mark.asyncio
    async def test_capacity_rejection_does_not_reserve_key(
        self, adapter, tmp_path
    ):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        with patch.object(
            adapter,
            "_concurrency_limited_response",
            side_effect=[
                web.json_response({"error": "full"}, status=429),
                None,
            ],
        ):
            async with TestClient(TestServer(app)) as cli:
                headers = {"Idempotency-Key": "capacity-retry"}
                rejected = await cli.post(
                    "/v1/runs", json={"input": "valid"}, headers=headers
                )
                with patch.object(adapter, "_create_agent") as create:
                    agent = MagicMock()
                    agent.run_conversation.return_value = {"final_response": "done"}
                    agent.session_prompt_tokens = agent.session_completion_tokens = (
                        agent.session_total_tokens
                    ) = 0
                    create.return_value = agent
                    accepted = await cli.post(
                        "/v1/runs", json={"input": "valid"}, headers=headers
                    )
        assert rejected.status == 429
        assert accepted.status == 202

    @pytest.mark.asyncio
    async def test_sequential_duplicate_reuses_original(self, adapter, tmp_path):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        calls = 0
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()

                def run(**kwargs):
                    nonlocal calls
                    calls += 1
                    return {"final_response": "done"}

                agent.run_conversation.side_effect = run
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                headers = {"Idempotency-Key": "retry-1"}
                first = await cli.post(
                    "/v1/runs", json={"input": "hello"}, headers=headers
                )
                second = await cli.post(
                    "/v1/runs", json={"input": "hello"}, headers=headers
                )
                assert first.status == second.status == 202
                assert (await first.json())["run_id"] == (await second.json())["run_id"]
                assert second.headers["Idempotency-Replayed"] == "true"
                await asyncio.sleep(0.1)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_changed_payload_conflicts(self, adapter, tmp_path):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                headers = {"Idempotency-Key": "same-key"}
                assert (
                    await cli.post("/v1/runs", json={"input": "one"}, headers=headers)
                ).status == 202
                conflict = await cli.post(
                    "/v1/runs", json={"input": "two"}, headers=headers
                )
                assert conflict.status == 409
                assert (await conflict.json())["error"][
                    "code"
                ] == "idempotency_key_conflict"

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_starts_once(self, adapter, tmp_path):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        calls = 0
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()

                def run(**kwargs):
                    nonlocal calls
                    calls += 1
                    time.sleep(0.05)
                    return {"final_response": "done"}

                agent.run_conversation.side_effect = run
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent

                async def post():
                    response = await cli.post(
                        "/v1/runs",
                        json={"input": "race"},
                        headers={"Idempotency-Key": "race-key"},
                    )
                    return response.status, await response.json()

                results = await asyncio.gather(*[post() for _ in range(8)])
                assert {status for status, _ in results} == {202}
                assert len({body["run_id"] for _, body in results}) == 1
                await asyncio.sleep(0.15)
        assert calls == 1

    def test_restart_durability_and_terminal_semantics(self, tmp_path):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        path = tmp_path / "idem.db"
        for terminal in ("completed", "failed", "cancelled"):
            first = RunIdempotencyStore(str(path))
            run_id = f"run_{terminal}"
            assert (
                first.reserve(
                    "tenant",
                    terminal,
                    "fp",
                    run_id,
                    {"run_id": run_id, "status": terminal},
                )[0]
                == "created"
            )
            first.close()
            restarted = RunIdempotencyStore(str(path))
            outcome, record = restarted.reserve(
                "tenant", terminal, "fp", "run_new", {"status": "queued"}
            )
            assert outcome == "reused"
            assert record["run_id"] == run_id
            assert record["status"]["status"] == terminal
            restarted.close()

    def test_tenant_isolation_and_retention(self, tmp_path):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        store = RunIdempotencyStore(str(tmp_path / "idem.db"))
        assert (
            store.reserve("tenant-a", "key", "fp-a", "run_a", {"status": "queued"})[0]
            == "created"
        )
        assert (
            store.reserve("tenant-b", "key", "fp-b", "run_b", {"status": "queued"})[0]
            == "created"
        )
        store.close()

    def test_retention_never_releases_an_active_idempotency_reservation(
        self, tmp_path
    ):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        store = RunIdempotencyStore(str(tmp_path / "idem.db"))
        with patch("gateway.platforms.api_server.time.time", return_value=100):
            assert store.reserve(
                "tenant",
                "active-key",
                "active-fingerprint",
                "run-active",
                {"status": "running"},
            )[0] == "created"
            assert store.reserve(
                "tenant",
                "done-key",
                "done-fingerprint",
                "run-done",
                {"status": "completed"},
            )[0] == "created"

        after_retention = 100 + RunIdempotencyStore.RETENTION_SECONDS + 1
        with patch(
            "gateway.platforms.api_server.time.time", return_value=after_retention
        ):
            active, active_record = store.lookup(
                "tenant", "active-key", "active-fingerprint"
            )
            done, done_record = store.lookup(
                "tenant", "done-key", "done-fingerprint"
            )

        assert active == "reused"
        assert active_record["run_id"] == "run-active"
        assert done == "missing"
        assert done_record is None
        store.close()

    def test_room_terminal_receipt_survives_offline_home_until_grant_horizon(
        self, tmp_path, monkeypatch
    ):
        from gateway.platforms import api_server_run_idempotency as idempotency

        now = [100.0]
        monkeypatch.setattr(idempotency.time, "time", lambda: now[0])
        store = idempotency.RunIdempotencyStore(str(tmp_path / "idem.db"))
        horizon = now[0] + 30 * 24 * 60 * 60
        assert store.reserve(
            "room-scope",
            "room:task-1:1",
            "room-fingerprint",
            "run-room",
            {"run_id": "run-room", "status": "completed"},
            retention_until=horizon,
        )[0] == "created"

        now[0] += idempotency.RunIdempotencyStore.RETENTION_SECONDS + 1
        store.reserve(
            "other-scope",
            "other-key",
            "other-fingerprint",
            "run-other",
            {"run_id": "run-other", "status": "queued"},
        )
        outcome, record = store.lookup(
            "room-scope",
            "room:task-1:1",
            "room-fingerprint",
        )
        assert outcome == "reused"
        assert record["run_id"] == "run-room"

        now[0] = horizon + 1
        store.reserve(
            "third-scope",
            "third-key",
            "third-fingerprint",
            "run-third",
            {"run_id": "run-third", "status": "queued"},
        )
        assert store.lookup(
            "room-scope",
            "room:task-1:1",
            "room-fingerprint",
        ) == ("missing", None)
        store.close()

    @pytest.mark.asyncio
    async def test_missing_key_preserves_legacy_new_run_behavior(
        self, adapter, tmp_path
    ):
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                first = await cli.post("/v1/runs", json={"input": "hello"})
                second = await cli.post("/v1/runs", json={"input": "hello"})
                assert (await first.json())["run_id"] != (await second.json())["run_id"]

    @pytest.mark.asyncio
    async def test_memory_scope_participates_in_fingerprint(
        self, auth_adapter, tmp_path
    ):
        adapter = auth_adapter
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                first_headers = {
                    "Authorization": "Bearer sk-secret",
                    "Idempotency-Key": "memory-scope",
                    "X-Hermes-Session-Key": "memory-a",
                }
                second_headers = {
                    "Authorization": "Bearer sk-secret",
                    "Idempotency-Key": "memory-scope",
                    "X-Hermes-Session-Key": "memory-b",
                }
                first = await cli.post(
                    "/v1/runs", json={"input": "same"}, headers=first_headers
                )
                conflict = await cli.post(
                    "/v1/runs", json={"input": "same"}, headers=second_headers
                )
                assert first.status == 202
                assert conflict.status == 409

    @pytest.mark.asyncio
    async def test_replay_bypasses_concurrency_limit_and_preserves_session_header(
        self, auth_adapter, tmp_path
    ):
        adapter = auth_adapter
        _use_idempotency_db(adapter, tmp_path / "idem.db")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                headers = {
                    "Authorization": "Bearer sk-secret",
                    "Idempotency-Key": "lost-acceptance",
                    "X-Hermes-Session-Key": "memory-a",
                }
                first = await cli.post(
                    "/v1/runs", json={"input": "same"}, headers=headers
                )
                first_body = await first.json()
                with patch.object(
                    adapter,
                    "_concurrency_limited_response",
                    return_value=web.json_response({"error": "full"}, status=429),
                ):
                    replay = await cli.post(
                        "/v1/runs", json={"input": "same"}, headers=headers
                    )
                replay_body = await replay.json()
                assert replay.status == 202
                assert replay_body["run_id"] == first_body["run_id"]
                assert replay_body["replayed"] is True
                assert replay.headers["X-Hermes-Session-Key"] == "memory-a"

    @pytest.mark.asyncio
    async def test_direct_status_hydrates_after_adapter_restart(
        self, tmp_path
    ):
        path = tmp_path / "idem.db"
        first_adapter = _make_adapter()
        _use_idempotency_db(first_adapter, path)
        first_app = _create_runs_app(first_adapter)
        async with TestClient(TestServer(first_app)) as cli:
            with patch.object(first_adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.return_value = {"final_response": "done"}
                agent.session_prompt_tokens = agent.session_completion_tokens = (
                    agent.session_total_tokens
                ) = 0
                create.return_value = agent
                started = await cli.post(
                    "/v1/runs",
                    json={"input": "same"},
                    headers={"Idempotency-Key": "restart-status"},
                )
                run_id = (await started.json())["run_id"]
                for _ in range(40):
                    status = await cli.get(f"/v1/runs/{run_id}")
                    if (await status.json()).get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)
        first_adapter._run_idempotency_store.close()

        restarted = _make_adapter()
        _use_idempotency_db(restarted, path)
        restarted_app = _create_runs_app(restarted)
        async with TestClient(TestServer(restarted_app)) as cli:
            status = await cli.get(f"/v1/runs/{run_id}")
            body = await status.json()
        assert status.status == 200
        assert body["status"] == "completed"
        assert body["output"] == "done"

    @pytest.mark.asyncio
    async def test_dead_owner_nonterminal_status_becomes_interrupted(
        self, tmp_path
    ):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

        path = tmp_path / "idem.db"
        scope = hashlib.sha256(
            b"default\0unauthenticated-test-listener"
        ).hexdigest()
        store = RunIdempotencyStore(str(path))
        store.reserve(
            scope,
            "stale-run",
            "fingerprint",
            "run_stale",
            {"run_id": "run_stale", "status": "running"},
            owner_pid=999_999_999,
            owner_started=1,
        )
        store.close()

        restarted = _make_adapter()
        _use_idempotency_db(restarted, path)
        app = _create_runs_app(restarted)
        async with TestClient(TestServer(app)) as cli:
            response = await cli.get("/v1/runs/run_stale")
            body = await response.json()
        assert response.status == 200
        assert body["status"] == "interrupted"
        assert body["last_event"] == "run.interrupted"


    def test_status_sweep_prunes_in_memory_ownership_mirrors(self, adapter):
        adapter._run_statuses["run_old"] = {
            "status": "completed",
            "updated_at": 1,
        }
        adapter._run_idempotency_ids.add("run_old")
        adapter._run_owners["run_old"] = "scope"

        adapter._sweep_orphaned_runs_once(adapter._RUN_STATUS_TTL + 2)

        assert "run_old" not in adapter._run_statuses
        assert "run_old" not in adapter._run_idempotency_ids
        assert "run_old" not in adapter._run_owners


class TestRunEventsHeadFlush:
    """The SSE head must reach the client before the first event (#80757).

    ``_handle_run_events`` used to call ``prepare()`` and go straight into the
    queue wait. aiohttp keeps the headers in the socket buffer until the first
    body write, so a subscriber that connects before the run emits anything got
    no bytes at all — ``fetch()``/``EventSource`` never resolve and the client
    looks hung. The approval flow is the worst case: ``approval.request`` only
    fires after the model thinks, so the client waits that whole time (or the
    30s keepalive) for headers that were ready immediately.
    """

    @pytest.mark.asyncio
    async def test_head_arrives_before_any_event(self, adapter):
        """A subscriber on a silent run reads its first byte immediately."""
        app = _create_runs_app(adapter)
        run_id = "run_silent_head"
        # A registered run whose queue stays empty for the whole test — the
        # exact shape of "subscribed before the first event was emitted".
        adapter._run_streams[run_id] = _RunStream()
        _claim_run(adapter, run_id)

        async with TestClient(TestServer(app)) as cli:
            resp = await asyncio.wait_for(
                cli.get(f"/v1/runs/{run_id}/events"), timeout=5.0
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")

            # Without the preamble this blocks until the 30s keepalive.
            first = await asyncio.wait_for(resp.content.read(1), timeout=3.0)
            assert first, "no body byte arrived before the first event"

            resp.close()


def _seed_legacy_run(path, body, key, run_id, *, state="claimed"):
    ledger = RunIdempotencyLedger(path)
    digest = (body.get("metadata") or {})["request_sha256"]
    payload = hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    ledger.claim(idempotency_key=key, request_sha256=digest,
                 payload_sha256=payload, proposed_run_id=run_id)
    if state != "claimed":
        ledger.update_state(run_id, state, {"run_id": run_id, "status": state,
                                           "result": "persisted before upgrade"})
    return ledger


def _install_test_run_store(adapter, path):
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
    adapter._run_idempotency_store.close()
    adapter._run_idempotency_store = RunIdempotencyStore(str(path))


@pytest.mark.asyncio
async def test_migrated_claim_is_tombstoned_before_worker_and_survives_retention(tmp_path):
    body = {"message": "once across upgrade", "metadata": {"request_sha256": "e" * 64}}
    key, run_id = "upgrade:retention", "run_upgrade_retention"
    ledger = _seed_legacy_run(tmp_path / "legacy.sqlite", body, key, run_id)
    adapter = _make_adapter()
    adapter._run_idempotency = ledger
    new_path = tmp_path / "new.sqlite"
    _install_test_run_store(adapter, new_path)
    agent = MagicMock()
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0

    observed_legacy_states = []

    def perform_once(**kwargs):
        observed_legacy_states.append(ledger.lookup_run(run_id).state)
        return {"final_response": "done"}

    agent.run_conversation.side_effect = perform_once
    with patch.object(adapter, "_create_agent", return_value=agent):
        async with TestClient(TestServer(_create_runs_app(adapter))) as client:
            response = await client.post("/v1/runs", json=body, headers={"Idempotency-Key": key})
            assert response.status == 202
            await asyncio.gather(*list(adapter._background_tasks))
            assert agent.run_conversation.call_count == 1
            assert observed_legacy_states == ["migrated"]
            status = await client.get(f"/v1/runs/{run_id}")
            assert (await status.json())["status"] == "completed"
    # Simulate an old completed receipt, then restart the API process. The
    # normal retention pruning is exercised by the next HTTP POST.
    adapter._run_idempotency_store._conn.execute(
        "UPDATE run_idempotency SET updated_at=0, acknowledged_at=0, retention_until=1"
    )
    adapter._run_idempotency_store._conn.commit()
    adapter._run_idempotency_store.close()
    replacement = _make_adapter()
    replacement._run_idempotency = ledger
    _install_test_run_store(replacement, new_path)
    with patch.object(replacement, "_create_agent") as create:
        async with TestClient(TestServer(_create_runs_app(replacement))) as client:
            response = await client.post("/v1/runs", json=body, headers={"Idempotency-Key": key})
            payload = await response.json()
            assert response.status == 202
            assert payload["run_id"] == run_id
            assert payload["deduplicated"] is True
            create.assert_not_called()
    replacement._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_migration_tombstone_refuses_reexecution_if_new_store_is_replaced(tmp_path):
    body = {"input": "already dispatched", "metadata": {"request_sha256": "f" * 64}}
    key, run_id = "upgrade:tombstone", "run_upgrade_tombstone"
    ledger = _seed_legacy_run(tmp_path / "old.sqlite", body, key, run_id, state="migrated")
    replacement = _make_adapter()
    replacement._run_idempotency = ledger
    _install_test_run_store(replacement, tmp_path / "empty-new.sqlite")
    with patch.object(replacement, "_create_agent") as create:
        async with TestClient(TestServer(_create_runs_app(replacement))) as client:
            response = await client.post("/v1/runs", json=body, headers={"Idempotency-Key": key})
            payload = await response.json()
            assert response.status == 409
            assert payload["run_id"] == run_id
            assert payload["status"] == "recovery_required"
            create.assert_not_called()
    replacement._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_idempotent_run_refuses_volatile_store_before_starting_worker(tmp_path):
    adapter = _make_adapter()
    _install_test_run_store(adapter, ":memory:")
    with patch.object(adapter, "_create_agent") as create:
        async with TestClient(TestServer(_create_runs_app(adapter))) as client:
            response = await client.post("/v1/runs", json={"input": "do not lose identity"},
                                         headers={"Idempotency-Key": "durable-required"})
            assert response.status == 503
            create.assert_not_called()
            assert not adapter._active_run_tasks
    adapter._run_idempotency_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state, expected", [("completed", "completed"), ("attached", "recovery_required")])
async def test_get_legacy_receipt_without_post_requires_owner_auth(tmp_path, state, expected):
    body = {"input": "old run", "metadata": {"request_sha256": "a" * 64}}
    run_id = "run_before_upgrade"
    adapter = _make_adapter(api_key="owner-key")
    adapter._run_idempotency = _seed_legacy_run(
        tmp_path / "old.sqlite", body, "legacy:get", run_id, state=state)
    _install_test_run_store(adapter, tmp_path / "fresh.sqlite")
    with patch.object(adapter, "_create_agent") as create:
        async with TestClient(TestServer(_create_runs_app(adapter))) as client:
            denied = await client.get(f"/v1/runs/{run_id}")
            assert denied.status == 401
            response = await client.get(f"/v1/runs/{run_id}",
                                        headers={"Authorization": "Bearer owner-key"})
            payload = await response.json()
            assert response.status == 200
            assert payload["run_id"] == run_id
            assert payload["status"] == expected
            create.assert_not_called()
    adapter._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_room_grant_cannot_read_legacy_owner_receipt(tmp_path):
    body = {"input": "owner work", "metadata": {"request_sha256": "b" * 64}}
    run_id = "run_legacy_owner_only"
    adapter = _make_adapter(api_key="owner-key")
    adapter._run_idempotency = _seed_legacy_run(
        tmp_path / "legacy.sqlite", body, "owner:only", run_id, state="completed")
    _install_test_run_store(adapter, tmp_path / "new.sqlite")
    async with TestClient(TestServer(_create_runs_app(adapter))) as client:
        invitation = await client.post(
            "/v1/room-members/invitations",
            json={"room_id": "review-room", "home_install_id": "home-install",
                  "authority_gateway_id": "home-gateway", "authority_epoch": 1,
                  "member_id": "review-member"},
            headers={"Authorization": "Bearer owner-key"})
        assert invitation.status < 300
        grant = (await invitation.json())["grant"]
        response = await client.get(f"/v1/runs/{run_id}",
                                    headers={"Authorization": f"HermesRoom {grant}"})
        assert response.status == 404
        owner_response = await client.get(f"/v1/runs/{run_id}",
                                          headers={"Authorization": "Bearer owner-key"})
        assert owner_response.status == 200
        assert (await owner_response.json())["result"] == "persisted before upgrade"
    adapter._run_idempotency_store.close()


# Shared fixtures are registered through pytest, not imported as unused names.
pytest_plugins = ('tests.gateway.test_api_server_runs',)
