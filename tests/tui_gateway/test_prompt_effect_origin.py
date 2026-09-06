"""Authenticated Desktop ingress must carry origin into the real executor."""
import threading
import pytest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent import durable_admission as admission
from hermes_cli import web_server
from hermes_state import SessionDB
from tui_gateway import server


@pytest.mark.parametrize("replayed_text", [None, "fixture input", "different input"])
@pytest.mark.parametrize("scenario", ["token", "ticket", "internal", "stdio", "missing", "malformed", "wrong-profile", "off", "fifo", "crossprofile"])
def test_authenticated_prompt_reaches_executor_with_origin(tmp_path, monkeypatch, replayed_text, scenario):
    profile = "other" if scenario == "wrong-profile" else "projetospessoais"
    home = tmp_path / "profiles" / profile
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "agent:\n  durable_admission:\n    mode: observe\n    tenant: personal\n    machine: mini\n"
        "    observation:\n      code_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KERNEL_V1_MODE", "off" if scenario == "off" else "observe")
    receipts = tmp_path / "receipts"
    monkeypatch.setenv("HERMES_KERNEL_SHADOW_RECEIPT_DIR", str(receipts))
    monkeypatch.setattr(admission, "_OBSERVER", None)
    monkeypatch.setattr(admission, "_OBSERVER_HEARTBEAT", None)
    monkeypatch.setattr(admission, "_OBSERVER_STOP", threading.Event())
    from hermes_cli import build_info
    monkeypatch.setattr(build_info, "get_code_identity", lambda: {"sha": "a" * 40})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(web_server.app.state, "auth_required", scenario in {"ticket", "internal"}, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    reached = threading.Event()
    observed = []
    checkpoints = []
    release_first = threading.Event()
    second_reached = threading.Event()
    resumed_origins = []

    def model_turn(*args, **kwargs):
        observed.append(admission.current_effect_origin())
        checkpoints.append((receipts / "observation-channel.json").exists())
        reached.set()
        if len(observed) == 2:
            second_reached.set()
        if scenario in {"fifo", "crossprofile"} and len(observed) == 1:
            assert release_first.wait(15)
            resumed_origins.append(admission.current_effect_origin())
        return {"messages": [], "final_response": "fixture result"}

    db = SessionDB(db_path=home / "state.db")
    db.create_session("native-origin-session", source="desktop")
    agent = SimpleNamespace(
        model="fixture-model", session_id="native-origin-session", quiet_mode=True,
        _session_db=db, _session_title_hint="fixture", run_conversation=model_turn,
    )
    session = {
        "session_key": "native-origin-session", "profile_home": str(home),
        "history": [], "history_lock": threading.Lock(), "history_version": 0,
        "running": False, "agent": agent, "attached_images": [], "cols": 80,
        "active_session_lease": object(), "db_persisted": True,
    }
    server._sessions["native-origin"] = session
    other_db = None
    other_session = None
    if scenario == "crossprofile":
        other_home = tmp_path / "profiles" / "other"
        other_home.mkdir()
        (other_home / "config.yaml").write_text((home / "config.yaml").read_text())
        other_db = SessionDB(db_path=other_home / "state.db")
        other_db.create_session("other-session-key", source="desktop")
        other_agent = SimpleNamespace(**dict(vars(agent), _session_db=other_db, session_id="other-session-key"))
        other_session = dict(session, profile_home=str(other_home), session_key="other-session-key",
                             agent=other_agent, history=[], history_lock=threading.Lock())
        server._sessions["other-native"] = other_session
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket, internal_ws_credential
    credential = "token=" + web_server._SESSION_TOKEN
    if scenario == "ticket":
        credential = "ticket=" + mint_ticket(user_id="fixture-owner", provider="fixture")
    elif scenario == "internal":
        credential = "internal=" + internal_ws_credential()
    source_id = None if scenario == "missing" else "invalid" if scenario == "malformed" else "11111111-1111-4111-8111-111111111111"
    try:
        client = TestClient(web_server.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
        with client.websocket_connect("ws://127.0.0.1/api/ws?" + credential) as ws:
            ws.receive_json()
            request = {"jsonrpc": "2.0", "id": "submit", "method": "prompt.submit", "params": {
                "session_id": "native-origin", "text": "fixture input",
                "source_event_id": source_id, "profile": "forged-profile", "source": "forged",
            }}
            if scenario == "stdio":
                server.handle_request(request)
            else:
                ws.send_json(request)
            assert reached.wait(15), "authenticated dispatch did not reach executor"
            if scenario not in {"token", "ticket", "fifo", "crossprofile"}:
                assert observed[0] is None
                if scenario in {"wrong-profile", "off"}:
                    assert not receipts.exists()
                return
            assert observed[0] is not None
            assert observed[0].session_id == "native-origin-session"
            assert observed[0].profile == "projetospessoais"
            assert checkpoints == [True], "serve observer must start before the model"
            if scenario == "crossprofile":
                ws.send_json({"jsonrpc": "2.0", "id": "other", "method": "prompt.submit", "params": {
                    "session_id": "other-native", "text": "fixture input",
                    "source_event_id": "22222222-2222-4222-8222-222222222222", "profile": "projetospessoais",
                }})
                assert second_reached.wait(15)
                assert observed[1] is None
                release_first.set()
                session["_run_thread"].join(15)
                assert resumed_origins == [observed[0]]
                return
            if scenario == "fifo":
                ws.send_json({"jsonrpc": "2.0", "id": "second", "method": "prompt.submit", "params": {
                    "session_id": "native-origin", "text": "fixture input",
                    "source_event_id": "22222222-2222-4222-8222-222222222222",
                }})
                while True:
                    response = ws.receive_json()
                    if response.get("id") == "second":
                        assert response["result"]["status"] == "queued"
                        break
                release_first.set()
                assert second_reached.wait(15)
                assert observed[1] is not None and observed[1].event_id != observed[0].event_id
                return
            if replayed_text is not None:
                session["_run_thread"].join(15)
                reached.clear()
                ws.send_json({"jsonrpc": "2.0", "id": "replay", "method": "prompt.submit", "params": {
                    "session_id": "native-origin", "text": replayed_text,
                    "source_event_id": "11111111-1111-4111-8111-111111111111",
                }})
                assert reached.wait(15)
                if replayed_text == "fixture input":
                    assert observed[1] == observed[0]
                else:
                    assert observed[1] is None, "changed content must never acquire another authority"
                    assert admission._OBSERVER.broken
    finally:
        release_first.set()
        thread = session.get("_run_thread")
        if thread:
            thread.join(15)
        server._sessions.pop("native-origin", None)
        if other_session is not None:
            other_thread = other_session.get("_run_thread")
            if other_thread:
                other_thread.join(15)
            server._sessions.pop("other-native", None)
            other_db.close()
        db.close()
        admission.stop_observation_heartbeat()
