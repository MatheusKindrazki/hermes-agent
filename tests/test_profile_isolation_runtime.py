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
import threading
from pathlib import Path

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
        assert b_seen.endswith("state/rich_sent_index.json")


# ---------------------------------------------------------------------------
# M2 — thread / executor context propagation
# ---------------------------------------------------------------------------

class TestThreadContextPropagation:
    """Worker threads must inherit the spawning turn's profile override."""

    def test_raw_thread_loses_override(self, two_profiles):
        """Document the underlying hazard: a bare thread does NOT inherit it."""
        _prof_a, prof_b = two_profiles
        seen = {}

        def worker():
            seen["home"] = str(get_hermes_home())

        def run():
            t = threading.Thread(target=worker)
            t.start()
            t.join()

        _under_override(prof_b, run)
        # A bare thread falls back to the process default — this is WHY the fix
        # primitive is needed.  (Asserted as the hazard, not the desired state.)
        assert seen["home"] != str(prof_b)


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
