from __future__ import annotations


def test_active_context_receipt_uses_only_explicit_backend_authority():
    from gateway.active_context_receipt import build_active_context_receipt

    config = {
        "gateway": {
            "reliability": {
                "active_context": {
                    "enabled": True,
                    "connection_id": "mini-direct",
                    "tenant": "lugui",
                    "machine": "personal-mac-mini",
                    "gateway_generation": "gateway-sha",
                }
            }
        }
    }

    assert build_active_context_receipt(
        config=config,
        profile="luguistaff",
        session={"id": "session-a"},
    ) == {
        "schema": "kindra.active-context/v1",
        "connection_id": "mini-direct",
        "profile": "luguistaff",
        "tenant": "lugui",
        "machine": "personal-mac-mini",
        "gateway_generation": "gateway-sha",
        "runtime_session_id": "session-a",
        "stored_session_id": "session-a",
        "xirp_session_id": None,
        "work_id": None,
    }


def test_active_context_receipt_is_absent_when_default_off_or_identity_missing():
    from gateway.active_context_receipt import build_active_context_receipt

    session = {"id": "session-a"}
    assert build_active_context_receipt(config={}, profile="luguistaff", session=session) is None
    assert (
        build_active_context_receipt(
            config={
                "gateway": {
                    "reliability": {
                        "active_context": {
                            "enabled": True,
                            "tenant": "lugui",
                        }
                    }
                }
            },
            profile="luguistaff",
            session=session,
        )
        is None
    )
