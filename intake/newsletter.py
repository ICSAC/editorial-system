"""The ICSAC / Persistence newsletter list (2026-09-30, the curator's spec).

Before this there was no list: the membership page promised an opt-in
newsletter and "newsletter" existed only as a promotion EXCLUSION on the
approval page. The submission forms now carry an optional, unticked box; a
tick is recorded here with the wording version the author saw and the time.

The list is ONE append-only JSONL file of events,
    ~/icsac-submissions/newsletter/subscribers.jsonl
(inside the nightly encrypted Drive backup as opi3b-icsac-submissions.tar.gz,
backup.sh:344). A row is either
    {"event": "subscribe", "email", "name", "sub_id", "consented_at", "wording_version", "source"}
or  {"event": "unsubscribe", "email", "at", "via"}
and the current list is the fold of the events, latest wins. Nothing is ever
rewritten. Test-tier submissions never write here.

Sending is a later build; whatever sends must use current_list(), put an
unsubscribe link (unsubscribe_token / /api/newsletter/unsubscribe) and ICSAC
LLC's postal address in every message (CAN-SPAM), and go out through the
existing author mail path at this scale.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Optional

WORDING_VERSION = "2026-09-30"


def list_path(submissions_root: Path) -> Path:
    return Path(submissions_root) / "newsletter" / "subscribers.jsonl"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def subscribe(submissions_root: Path, *, email: str, name: str, sub_id: str,
              wording_version: str, consented_at: Optional[str] = None,
              source: str = "submission-form") -> dict:
    """Record a tick. Idempotent in effect (the fold keeps the latest event)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("email required")
    row = {"event": "subscribe", "email": email, "name": (name or "").strip()[:200],
           "sub_id": sub_id, "consented_at": consented_at or _now(),
           "wording_version": wording_version or WORDING_VERSION, "source": source}
    _append(list_path(submissions_root), row)
    return row


def unsubscribe(submissions_root: Path, *, email: str, via: str = "link") -> dict:
    email = (email or "").strip().lower()
    row = {"event": "unsubscribe", "email": email, "at": _now(), "via": via}
    _append(list_path(submissions_root), row)
    return row


def events(submissions_root: Path) -> list[dict]:
    p = list_path(submissions_root)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def current_list(submissions_root: Path) -> list[dict]:
    """Subscribed addresses after folding the events, latest event wins."""
    state: dict[str, dict] = {}
    for e in events(submissions_root):
        em = (e.get("email") or "").lower()
        if not em:
            continue
        if e.get("event") == "subscribe":
            state[em] = e
        elif e.get("event") == "unsubscribe":
            state.pop(em, None)
    return sorted(state.values(), key=lambda r: r.get("consented_at") or "")


def is_subscribed(submissions_root: Path, email: str) -> bool:
    em = (email or "").strip().lower()
    return any(r["email"] == em for r in current_list(submissions_root))


def unsubscribe_token(secret: bytes, email: str) -> str:
    """Opaque token for the unsubscribe link: hex(email) + HMAC over it. The
    email is recoverable by the server only after the MAC checks."""
    em = (email or "").strip().lower().encode()
    mac = hmac.new(secret, b"newsletter-unsubscribe\n" + em, hashlib.sha256).hexdigest()[:32]
    return em.hex() + "." + mac


def email_from_token(secret: bytes, token: str) -> Optional[str]:
    try:
        hexed, mac = (token or "").split(".", 1)
        em = bytes.fromhex(hexed)
    except ValueError:
        return None
    want = hmac.new(secret, b"newsletter-unsubscribe\n" + em, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(want, mac):
        return None
    return em.decode(errors="replace")
