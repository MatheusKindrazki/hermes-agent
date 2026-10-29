"""Stable internal input identity before crash-resilient turn persistence."""
from agent.conversation_compression_spool import ensure_compression_turn_identity


def stage_turn_input_identity(agent, user_message, persist_user_message,
                              persist_user_timestamp, persist_user_display_kind,
                              persist_user_display_metadata):
    # Bind a durable, per-input identity before build_turn_context performs
    # its crash-resilience append. The staged mapping is the same object the
    # turn prologue adopts, so the ID reaches both SessionDB display metadata
    # and a later compression spool without changing provider-visible content.
    _expected_persist_content = (
        persist_user_message if persist_user_message is not None else user_message
    )
    _staged_user = getattr(agent, "_pending_cli_user_message", None)
    if not (
        isinstance(_staged_user, dict)
        and _staged_user.get("content") == _expected_persist_content
    ):
        _staged_user = {"role": "user", "content": _expected_persist_content}
        if persist_user_timestamp is not None:
            _staged_user["timestamp"] = persist_user_timestamp
        agent._pending_cli_user_message = _staged_user
    _typed_metadata = (
        dict(persist_user_display_metadata)
        if isinstance(persist_user_display_metadata, dict)
        else {}
    )
    _typed_metadata.pop("_compression_turn_id", None)
    _staged_metadata = _staged_user.get("display_metadata")
    if not isinstance(_staged_metadata, dict):
        _staged_metadata = {}
    else:
        _staged_metadata = dict(_staged_metadata)
    _staged_metadata.update(_typed_metadata)
    _staged_user["display_metadata"] = _staged_metadata
    ensure_compression_turn_identity(_staged_user)
    if persist_user_display_kind or persist_user_display_metadata is not None:
        # turn_context applies typed metadata after adopting this staged dict;
        # pass the merged mapping so that assignment retains the internal ID.
        persist_user_display_metadata = _staged_user["display_metadata"]

    return persist_user_display_metadata


def refresh_turn_credentials(agent):
    """Adopt saved credentials between turns without disrupting a turn on refresh failure."""
    import logging
    try:
        agent._try_refresh_env_client_credentials()
    except Exception:
        logging.getLogger(__name__).debug("per-turn env credential refresh failed", exc_info=True)
