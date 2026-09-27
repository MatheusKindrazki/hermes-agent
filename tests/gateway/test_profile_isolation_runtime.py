"""Profile-isolation regression tests for single-process multi-profile runtimes.

In runtimes that serve every profile from one OS process (the desktop
``tui_gateway``), the profile boundary is the context-local
``_HERMES_HOME_OVERRIDE`` ContextVar, not the process environment.  State that
escapes the request call stack — import-time-frozen path constants, direct
``os.environ`` reads, or worker threads that don't inherit the request context —
silently reverts to the launch/default profile and leaks one profile's data
into another.

These tests drive each previously-leaking site under override A then override B
with real temp HERMES_HOME directories (no mocks) and assert the *active*
profile's path is used.  They are the productionized form of the manual smoke
probes used to confirm the bug class.
"""

import asyncio
import concurrent.futures
import threading
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from agent.runtime_cwd import clear_session_cwd, resolve_agent_cwd, set_session_cwd
from agent.secret_scope import get_secret, reset_secret_scope, set_secret_scope
from gateway.turn_context import (
    TurnContext,
    compose_request_context,
    current_request_context,
    request_context_scope,
    request_context_v2_enabled,
)
from gateway.config import Platform
from gateway.run import TurnRunner


@pytest.fixture
def two_profiles(tmp_path):
    """Two distinct profile HERMES_HOME dirs with the dir skeleton created."""
    prof_a = tmp_path / "profA"
    prof_b = tmp_path / "profB"
    for p in (prof_a, prof_b):
        (p / "skills").mkdir(parents=True, exist_ok=True)
        (p / "state").mkdir(parents=True, exist_ok=True)
        (p / "cache").mkdir(parents=True, exist_ok=True)
    return prof_a, prof_b


def _under_override(home: Path, fn):
    """Run ``fn`` with the profile override set to ``home`` and reset after."""
    token = set_hermes_home_override(str(home))
    try:
        return fn()
    finally:
        reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# M1 — import-time path globals / direct os.environ reads
# ---------------------------------------------------------------------------

class TestSkillsHubPathResolution:
    """tools/skills_hub.py path constants must reflect the active profile."""

    def test_skills_dir_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import tools.skills_hub as sh

        # Importing/touching under A must NOT pin the path for B.
        a_seen = _under_override(prof_a, lambda: Path(sh.SKILLS_DIR))
        b_seen = _under_override(prof_b, lambda: Path(sh.SKILLS_DIR))

        assert a_seen == prof_a / "skills"
        assert b_seen == prof_b / "skills"
        assert a_seen != b_seen

    def test_hub_derived_paths_follow_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import tools.skills_hub as sh

        b_lock = _under_override(prof_b, lambda: Path(sh.LOCK_FILE))
        b_audit = _under_override(prof_b, lambda: Path(sh.AUDIT_LOG))
        b_index = _under_override(prof_b, lambda: Path(sh.INDEX_CACHE_DIR))

        assert b_lock == prof_b / "skills" / ".hub" / "lock.json"
        assert b_audit == prof_b / "skills" / ".hub" / "audit.log"
        assert b_index == prof_b / "skills" / ".hub" / "index-cache"



class TestGatewayCacheDirResolution:
    """gateway/platforms/base.py cache getters must follow the active profile."""

    def test_image_cache_dir_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import gateway.platforms.base as gb

        a_seen = _under_override(prof_a, lambda: gb.get_image_cache_dir())
        b_seen = _under_override(prof_b, lambda: gb.get_image_cache_dir())

        assert str(a_seen).startswith(str(prof_a))
        assert str(b_seen).startswith(str(prof_b))
        assert a_seen != b_seen




class TestRichSentStorePathResolution:
    """gateway/rich_sent_store.py must honor the override, not read os.environ."""

    def test_store_path_follows_override(self, two_profiles, monkeypatch):
        prof_a, prof_b = two_profiles
        # Ensure no ambient HERMES_HOME env masks the test.
        monkeypatch.delenv("HERMES_HOME", raising=False)
        import gateway.rich_sent_store as rss

        b_seen = _under_override(prof_b, lambda: rss._store_path())
        assert b_seen.startswith(str(prof_b))
        assert b_seen.endswith(os.path.join("state", "rich_sent_index.json"))


class TestGatewayHooksDirResolution:
    """gateway/hooks.py's HookRegistry must discover hooks from the active
    profile's directory — otherwise one profile's HookRegistry loads and
    executes a DIFFERENT profile's hook handlers (arbitrary Python) against
    its own live event context under the multiplexed gateway."""

    def test_hooks_dir_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import gateway.hooks as gh

        a_seen = _under_override(prof_a, lambda: gh._resolve_hooks_dir())
        b_seen = _under_override(prof_b, lambda: gh._resolve_hooks_dir())

        assert a_seen == prof_a / "hooks"
        assert b_seen == prof_b / "hooks"
        assert a_seen != b_seen

    def test_discover_and_load_uses_active_profile_hooks_dir(self, two_profiles):
        """End-to-end: a hook that only exists under profile B's hooks dir
        must not be discovered when profile A's override is active, and vice
        versa — proving the registry doesn't fall back to a frozen profile."""
        prof_a, prof_b = two_profiles
        import gateway.hooks as gh

        b_hook_dir = prof_b / "hooks" / "only-in-b"
        b_hook_dir.mkdir(parents=True)
        (b_hook_dir / "HOOK.yaml").write_text(
            "name: only-in-b\nevents: [\"agent:start\"]\n", encoding="utf-8",
        )
        (b_hook_dir / "handler.py").write_text(
            "async def handle(event_type, context):\n    pass\n", encoding="utf-8",
        )

        def _load_and_names():
            reg = gh.HookRegistry()
            reg.discover_and_load()
            return [h["name"] for h in reg.loaded_hooks]

        a_hooks = _under_override(prof_a, _load_and_names)
        b_hooks = _under_override(prof_b, _load_and_names)

        assert "only-in-b" not in a_hooks
        assert "only-in-b" in b_hooks


class TestCheckpointManagerPathResolution:
    """tools/checkpoint_manager.py's checkpoint store root must honor the
    active profile — otherwise one profile's CheckpointManager instance can
    read/write code-edit checkpoints into a different profile's store under
    the multiplexed gateway."""

    def test_checkpoint_base_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import tools.checkpoint_manager as cm

        a_seen = _under_override(prof_a, lambda: cm._resolve_checkpoint_base())
        b_seen = _under_override(prof_b, lambda: cm._resolve_checkpoint_base())

        assert a_seen == prof_a / "checkpoints"
        assert b_seen == prof_b / "checkpoints"
        assert a_seen != b_seen

    def test_store_path_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import tools.checkpoint_manager as cm

        b_seen = _under_override(prof_b, lambda: cm._store_path())
        assert b_seen == prof_b / "checkpoints" / "store"


class TestStickerCachePathResolution:
    """gateway/sticker_cache.py's cache file must honor the active profile —
    otherwise one profile's Telegram sticker-description cache leaks into a
    different profile's under the multiplexed gateway."""

    def test_cache_path_follows_override(self, two_profiles):
        prof_a, prof_b = two_profiles
        import gateway.sticker_cache as sc

        a_seen = _under_override(prof_a, lambda: sc._resolve_cache_path())
        b_seen = _under_override(prof_b, lambda: sc._resolve_cache_path())

        assert a_seen == prof_a / "sticker_cache.json"
        assert b_seen == prof_b / "sticker_cache.json"
        assert a_seen != b_seen


# ---------------------------------------------------------------------------
# M2 — thread / executor context propagation
# ---------------------------------------------------------------------------

class TestThreadContextPropagation:
    """Worker threads must inherit the spawning turn's profile override."""



    def test_run_async_worker_preserves_override(self, two_profiles):
        """model_tools._run_async's worker-thread branch must keep the override.

        This is the generic sync->async bridge for every async tool; if it
        leaks, every async tool that resolves get_hermes_home() leaks.
        """
        import asyncio

        _prof_a, prof_b = two_profiles
        import model_tools

        async def reads_home():
            return str(get_hermes_home())

        async def driver():
            # Inside a running loop, _run_async spawns a worker thread + loop.
            return model_tools._run_async(reads_home())

        seen = _under_override(prof_b, lambda: asyncio.run(driver()))
        assert seen == str(prof_b)


class TestRequestContextV2Isolation:
    @staticmethod
    def _config(enabled: bool) -> dict:
        return {
            "gateway": {
                "reliability": {
                    "request_context": {"enabled": enabled},
                }
            }
        }

    def test_request_context_v2_is_default_off(self):
        assert request_context_v2_enabled({}) is False
        assert request_context_v2_enabled(self._config(True)) is True

    @pytest.mark.asyncio
    async def test_concurrent_requests_keep_all_authority_fields_isolated(
        self, two_profiles, tmp_path
    ):
        prof_a, prof_b = two_profiles
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        both_bound = asyncio.Event()
        arrivals = 0
        arrivals_lock = asyncio.Lock()

        async def run_request(
            *,
            home: Path,
            workspace: Path,
            profile: str,
            tenant: str,
            model: str,
            provider: str,
            approval: str,
            api_key: str,
            session_id: str,
        ):
            nonlocal arrivals
            home_token = set_hermes_home_override(home)
            set_session_cwd(str(workspace))
            secret_token = set_secret_scope({"TEST_PROVIDER_KEY": api_key})
            try:
                request_context = compose_request_context(
                    profile=profile,
                    tenant=tenant,
                    model=model,
                    provider=provider,
                    approval=approval,
                    session_id=session_id,
                )
                turn = TurnContext(request_context=request_context)
                with request_context_scope(
                    request_context,
                    config=self._config(True),
                ):
                    async with arrivals_lock:
                        arrivals += 1
                        if arrivals == 2:
                            both_bound.set()
                    await asyncio.wait_for(both_bound.wait(), timeout=1)
                    current = current_request_context()
                    return {
                        "request": current,
                        "turn": turn.request_context,
                        "home": get_hermes_home(),
                        "workspace": resolve_agent_cwd(),
                        "api_key": get_secret("TEST_PROVIDER_KEY"),
                    }
            finally:
                reset_secret_scope(secret_token)
                clear_session_cwd()
                reset_hermes_home_override(home_token)

        first, second = await asyncio.gather(
            run_request(
                home=prof_a,
                workspace=workspace_a,
                profile="luguistaff",
                tenant="lugui",
                model="model-a",
                provider="provider-a",
                approval="approval-a",
                api_key="secret-a",
                session_id="session-a",
            ),
            run_request(
                home=prof_b,
                workspace=workspace_b,
                profile="applausestaff",
                tenant="applause",
                model="model-b",
                provider="provider-b",
                approval="approval-b",
                api_key="secret-b",
                session_id="session-b",
            ),
        )

        assert (
            first["request"].profile,
            first["request"].tenant,
            first["request"].model,
            first["request"].provider,
            first["request"].approval,
            first["request"].session_id,
            first["home"],
            first["workspace"],
            first["api_key"],
        ) == (
            "luguistaff",
            "lugui",
            "model-a",
            "provider-a",
            "approval-a",
            "session-a",
            prof_a,
            workspace_a,
            "secret-a",
        )
        assert (
            second["request"].profile,
            second["request"].tenant,
            second["request"].model,
            second["request"].provider,
            second["request"].approval,
            second["request"].session_id,
            second["home"],
            second["workspace"],
            second["api_key"],
        ) == (
            "applausestaff",
            "applause",
            "model-b",
            "provider-b",
            "approval-b",
            "session-b",
            prof_b,
            workspace_b,
            "secret-b",
        )
        assert first["turn"] is first["request"]
        assert second["turn"] is second["request"]
        assert first["request"] is not second["request"]

    def test_disabled_scope_does_not_publish_request_context(self, two_profiles):
        prof_a, _ = two_profiles
        home_token = set_hermes_home_override(prof_a)
        try:
            request_context = compose_request_context(
                profile="luguistaff",
                tenant="lugui",
                model="model-a",
                provider="provider-a",
                approval="approval-a",
                session_id="session-a",
            )
            with request_context_scope(request_context, config=self._config(False)):
                assert current_request_context() is None
        finally:
            reset_hermes_home_override(home_token)

    def test_real_turn_entrypoint_isolates_interleaved_request_authority(
        self, two_profiles, tmp_path
    ):
        """The real TurnRunner seam must publish authority before agent setup."""
        prof_a, prof_b = two_profiles
        workspace_a = tmp_path / "runtime-workspace-a"
        workspace_b = tmp_path / "runtime-workspace-b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        both_inside = threading.Barrier(2)

        class ProbeComplete(Exception):
            pass

        class ProbeGateway:
            def __init__(self, *, model: str, provider: str):
                self.model = model
                self.provider = provider
                self._provider_routing = None

            def _get_system_prompt_for_channel(self, *_args, **_kwargs):
                return None

            def _resolve_session_agent_runtime(self, **_kwargs):
                return self.model, {"provider": self.provider}

            def _resolve_session_reasoning_config(self, **_kwargs):
                both_inside.wait(timeout=2)
                self.seen = current_request_context()
                raise ProbeComplete

        def run_turn(
            *,
            home: Path,
            workspace: Path,
            profile: str,
            tenant: str,
            model: str,
            provider: str,
            approval: str,
            session_id: str,
        ):
            home_token = set_hermes_home_override(home)
            set_session_cwd(str(workspace))
            try:
                config = self._config(True)
                config["gateway"]["reliability"]["request_context"].update(
                    {
                        "profile": profile,
                        "tenant": tenant,
                        "approval": approval,
                    }
                )
                gateway = ProbeGateway(model=model, provider=provider)
                source = SimpleNamespace(
                    platform=Platform.LOCAL,
                    chat_id=session_id,
                    thread_id=None,
                    parent_chat_id=None,
                )
                ctx = TurnContext(
                    source=source,
                    user_config=config,
                    session_id=session_id,
                    session_key=session_id,
                    context_prompt=None,
                    channel_prompt=None,
                    resolve_display_setting=lambda *_args, **_kwargs: None,
                )
                try:
                    TurnRunner(gateway, ctx).run_sync()
                except ProbeComplete:
                    pass
                return gateway.seen, ctx.request_context
            finally:
                clear_session_cwd()
                reset_hermes_home_override(home_token)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                run_turn,
                home=prof_a,
                workspace=workspace_a,
                profile="luguistaff",
                tenant="lugui",
                model="model-a",
                provider="provider-a",
                approval="manual-a",
                session_id="session-a",
            )
            second_future = pool.submit(
                run_turn,
                home=prof_b,
                workspace=workspace_b,
                profile="applausestaff",
                tenant="applause",
                model="model-b",
                provider="provider-b",
                approval="manual-b",
                session_id="session-b",
            )
            first = first_future.result(timeout=4)
            second = second_future.result(timeout=4)

        assert first[0] is first[1]
        assert second[0] is second[1]
        assert (
            first[0].profile,
            first[0].tenant,
            first[0].model,
            first[0].provider,
            first[0].approval,
            first[0].session_id,
            first[0].hermes_home,
            first[0].workspace,
        ) == (
            "luguistaff",
            "lugui",
            "model-a",
            "provider-a",
            "manual-a",
            "session-a",
            prof_a,
            workspace_a,
        )
        assert (
            second[0].profile,
            second[0].tenant,
            second[0].model,
            second[0].provider,
            second[0].approval,
            second[0].session_id,
            second[0].hermes_home,
            second[0].workspace,
        ) == (
            "applausestaff",
            "applause",
            "model-b",
            "provider-b",
            "manual-b",
            "session-b",
            prof_b,
            workspace_b,
        )
        assert current_request_context() is None
