"""Pure-data factory for auxiliary model configuration defaults."""


def _aux(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary-task model block (see DEFAULT_CONFIG["auxiliary"]).

    reasoning_effort=False omits that key (MoA blocks configure depth per slot);
    ``extra`` keys are appended after the standard ones.
    """
    d = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d


def _operations_defaults():
    """Fresh opt-in operational integration defaults for each configuration."""
    return {
        # Fork operational integrations are opt-in and scoped to the owning profile.
        "operations": {
            "shadow_receipt_dir": "",
            "shadow_producer_enabled": False,
            "tone_gate_bin": "",
            "tone_schema_sha256": "",
            "work_control_projection_enabled": False,
            "work_control_projection_url": "",
        },
    }


def _session_defaults():
    """Fresh session continuation settings."""
    return {
        # Per-terminal `hermes -c`: each CLI session writes a breadcrumb under
        # $HERMES_HOME/terminal-sessions/<terminal-id>, so bare -c/--continue resumes THIS
        # terminal's session (tmux/kitty/wezterm pane, tty). false = resume globally most-recent.
        "terminal_continue": True,
    }
