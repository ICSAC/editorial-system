"""ET display formatting for ICSAC submission intake timestamps.

Storage policy: every timestamp on disk (submission.json, state.json,
audit-log.jsonl, review markdown frontmatter) stays as UTC ISO 8601
("YYYY-MM-DDTHH:MM:SSZ"). This is the machine-canonical form — sortable
lexicographically, parseable by every JSON tooling, immune to DST
ambiguity.

Display policy: when a timestamp surfaces to a human (state-page JSON
response, worker stdout, Telegram messages, intake-failure emails), it
is rendered as "MM/DD/YY HH:MM:SS EDT|EST" via to_et_display(). The
zone abbreviation tracks DST automatically — no assumptions about which
zone rule is active when the code runs.

Shared between intake_server.py and submission_worker.py so both sides
emit identical formats.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DISPLAY_FMT = "%m/%d/%y %H:%M:%S %Z"


def to_et_display(iso_utc_str: str | None) -> str | None:
    """Convert a UTC ISO 8601 string to 'MM/DD/YY HH:MM:SS EDT|EST'.

    Pass-through for None / empty input. Pass-through unchanged if the
    string isn't parseable (defense against malformed legacy entries —
    we'd rather show the raw value than crash a state response).
    """
    if not iso_utc_str:
        return iso_utc_str
    s = iso_utc_str.replace("Z", "+00:00")
    try:
        dt_utc = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return iso_utc_str
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(ET).strftime(DISPLAY_FMT)


def now_et_display() -> str:
    """Right-now in ET display format. Used for log-prefix in worker
    stdout — no UTC-to-ET round trip needed."""
    return datetime.now(ET).strftime(DISPLAY_FMT)


def now_utc_iso() -> str:
    """Canonical now-string for storage. Mirrors the inline helpers
    that already exist in intake_server.py / submission_worker.py /
    apply_decision.py — keeping a single shared definition here so
    storage and display formats live in the same module."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
