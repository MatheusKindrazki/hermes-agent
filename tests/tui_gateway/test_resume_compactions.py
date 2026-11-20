"""RFC-001 V1: ``session.resume`` reports the stored compaction count, and a parented seed stays hidden.

The voice client rotates its session after three compactions, but the resume projection strips every
compaction carrier and ``info.usage.compressions`` reads 0 on a cold resume, so the count has to come from
the stored transcript. Separately, a ``session.create`` with ``parent_session_id`` and a hidden seed (the
voice rotation) stored the seed without ``display_kind``, so the 16k-character handoff showed up as a
visible user turn on the next resume.
"""
import subprocess

from agent.context_compressor import LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.resume_compactions import CARRIER_PREFIXES, stored_compactions


def test_prefixes_match_the_compressor():
    assert SUMMARY_PREFIX.startswith(CARRIER_PREFIXES[0])
    assert LEGACY_SUMMARY_PREFIX.startswith(CARRIER_PREFIXES[1])


def test_counts_distinct_carriers_only(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("voice", source="iphone-voice")
        db.append_message("voice", "user", "oi", timestamp=1.0)
        db.append_message("voice", "user", SUMMARY_PREFIX + " resumo 1", timestamp=2.0)
        db.append_message("voice", "assistant", SUMMARY_PREFIX + " not a carrier: assistant role", timestamp=3.0)
        db.append_message("voice", "user", LEGACY_SUMMARY_PREFIX + " resumo legado", timestamp=4.0)
        db.create_session("other", source="desktop")
        db.append_message("other", "user", SUMMARY_PREFIX + " resumo de outra sessão", timestamp=5.0)
        assert stored_compactions(db, "voice") == 2
        assert stored_compactions(db, "other") == 1
        assert stored_compactions(db, "missing") == 0
        # A regenerated transcript keeps older copies of the same row (same message_uid, inactive):
        # one compaction, counted once.
        db._conn.execute("UPDATE messages SET message_uid = 'uid-1' WHERE session_id = 'voice' AND content LIKE ?",
                         (SUMMARY_PREFIX + "%",))
        db._conn.execute("INSERT INTO messages (session_id, role, content, timestamp, active, message_uid) "
                         "VALUES ('voice', 'user', ?, 6.0, 0, 'uid-1')", (SUMMARY_PREFIX + " resumo 1",))
        db._conn.commit()
        assert stored_compactions(db, "voice") == 2
    finally:
        db.close()


def test_unknown_is_none_never_zero():
    assert stored_compactions(None, "voice") is None
    assert stored_compactions(object(), "voice") is None  # a handle without _read_one
    assert stored_compactions(object(), "") is None


def _quiet(monkeypatch, db, tmp_path):
    monkeypatch.setattr("hermes_cli.banner.prefetch_update_check", lambda: None)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_load_cfg", dict)
    monkeypatch.setattr(server, "_profile_home", lambda *a: None)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(tmp_path))


def test_resume_reports_compactions_the_projection_hides(monkeypatch, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _quiet(monkeypatch, db, tmp_path)
    try:
        db.create_session("voice", source="iphone-voice")
        db.append_message("voice", "user", "primeira pergunta", timestamp=1.0)
        db.append_message("voice", "assistant", "primeira resposta", timestamp=2.0)
        for n in range(3):
            db.append_message("voice", "user", f"{SUMMARY_PREFIX}\n\nresumo {n}", timestamp=3.0 + n)
        db.append_message("voice", "user", "última pergunta", timestamp=10.0)
        db.append_message("voice", "assistant", "última resposta", timestamp=11.0)
        response = server.handle_request({"id": "resume", "method": "session.resume", "params": {
            "session_id": "voice", "source": "iphone-voice", "cols": 96}})
        assert "result" in response, response
        result = response["result"]
        assert result["compactions"] == 3
        # The count is needed because the wire never shows a carrier.
        assert not any(str(m.get("text") or "").startswith(CARRIER_PREFIXES[0]) for m in result["messages"])
    finally:
        db.close()


def test_resume_without_compactions_reports_zero(monkeypatch, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _quiet(monkeypatch, db, tmp_path)
    try:
        db.create_session("fresh", source="iphone-voice")
        db.append_message("fresh", "user", "oi", timestamp=1.0)
        db.append_message("fresh", "assistant", "olá", timestamp=2.0)
        result = server.handle_request({"id": "resume", "method": "session.resume", "params": {
            "session_id": "fresh", "source": "iphone-voice", "cols": 96}})["result"]
        assert result["compactions"] == 0
    finally:
        db.close()


def test_parented_seed_keeps_hidden_rows_hidden(monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    db = SessionDB(tmp_path / "state.db")
    _quiet(monkeypatch, db, tmp_path)
    try:
        db.create_session("old-voice", source="iphone-voice", cwd=str(project))
        seed = [{"role": "user", "content": "[Continuação da conversa de voz] resumo longo", "display_kind": "hidden"},
                {"role": "assistant", "content": "Entendido. Sigo a partir daqui.", "display_kind": "hidden"}]
        response = server._methods["session.create"]("create", {
            "source": "iphone-voice", "cwd": str(project), "parent_session_id": "old-voice", "messages": seed})
        assert "error" not in response, response
        child = response["result"]["stored_session_id"]
        rows = db.get_messages_as_conversation(child)
        assert [r["content"] for r in rows] == [m["content"] for m in seed]
        assert [r.get("display_kind") for r in rows] == ["hidden", "hidden"]
    finally:
        db.close()
