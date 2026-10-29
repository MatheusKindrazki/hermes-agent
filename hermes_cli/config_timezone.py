"""Validation of timezone configuration against the local IANA database."""
from __future__ import annotations
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from hermes_cli.config import ConfigIssue

def _validate_timezone(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """``timezone`` must be an IANA name the runtime can load.

    ``hermes_time._get_zoneinfo()`` swallows an invalid name behind a single WARNING in the
    gateway log, then runs the agent clock AND every cron schedule on server-local time.
    Surface it here, where doctor and the startup check both look. Silent when the
    interpreter has no tz database at all (bare Windows without ``tzdata``) — nothing can be
    judged there.
    """
    from hermes_cli.config import _issue

    if "timezone" not in config:
        return
    tz = config.get("timezone")
    hint = ("Use an IANA zone name such as America/New_York or Asia/Tokyo (see "
            "`timedatectl list-timezones`). With an invalid value the agent clock and cron "
            "schedules silently fall back to server-local time. HERMES_TIMEZONE overrides "
            "this key when set.")
    if tz is not None and not isinstance(tz, str):
        _issue(issues, "error", f"timezone must be an IANA zone name string, got {tz!r}", hint)
        return
    if not (isinstance(tz, str) and tz.strip()):
        return
    name = tz.strip()
    import zoneinfo
    try:
        zoneinfo.ZoneInfo("UTC")  # is a tz database available at all?
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return
    try:
        zoneinfo.ZoneInfo(name)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        _issue(issues, "error", f"timezone {name!r} is not a valid IANA zone name", hint)
