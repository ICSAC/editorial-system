"""Author approval and objection window for accepted submissions (2026-09-27).

The acceptance email gives the author a personal link and a date. The link
records ONE decision — approve (with any per-category exclusions), hold, or
withdraw — timestamped; the date closes an objection window. Neither publishes
anything. Both only reach the curation team as a Telegram ping, and
intake/register-doi.sh --live stays the human step; its gate reads this file.

Exclusions are all-or-nothing per category (no picking one platform out of a
category):
  social           social media, all platforms
  print_broadcast  print materials and broadcast or paid advertising
  newsletter       email newsletters and announcements
  web_features     website features beyond the paper's own record page
  persistence      the Persistence annual volume (print and ebook)

On disk: <sub_dir>/author_approval.json
  {token_sha256, issued_at, deadline, status, responses[], window_closed_pinged}
Only the token's hash is stored, so a copied state directory cannot be replayed.
state.json mirrors: author_approval_status, author_window_deadline,
author_exclusions, promotion_opt_out, persistence_opt_out, withdrawn_by_author,
author_hold_note.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import config  # noqa: E402

FILE = "author_approval.json"
SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
TEST_SUBMISSIONS_ROOT = SUBMISSIONS_ROOT / "test"
SUB_ID_RE = re.compile(r"^(ICSAC-SUB-TEST-\d+|ICSAC-SUB-\d{5})$")

# Single source of truth for the page (served by public_view) and the record.
# `label` is the sentence on the response page's tick box (an explicit "Do not",
# his 2026-09-28 rule: a ticked box must read as the instruction it gives);
# `short` is the noun for status lines and curator pings ("excluding: social media").
CATEGORIES = [
    {"id": "social", "group": "promotion", "short": "social media",
     "label": "Do not announce my paper on social media",
     "detail": "All platforms, at no cost to you. Unticked, we announce new papers with the title, author name and link."},
    {"id": "print_broadcast", "group": "promotion", "short": "print materials and advertising",
     "label": "Do not use my paper in print materials, broadcast or paid advertising",
     "detail": "Flyers, posters, conference materials, podcast or video mentions, paid placements. The Institute pays for all of it."},
    {"id": "newsletter", "group": "promotion", "short": "newsletters and email announcements",
     "label": "Do not include my paper in newsletters or email announcements",
     "detail": "Mentions in the Institute's newsletters to subscribers."},
    {"id": "web_features", "group": "promotion", "short": "website features",
     "label": "Do not feature my paper on the website beyond its own record page",
     "detail": "Homepage or news items about the paper. The record page itself is part of publication."},
    {"id": "persistence", "group": "publishing", "short": "the annual print and ebook edition",
     "label": "Do not include my paper in the annual print and ebook edition of Persistence",
     "detail": "The Institute's yearly paperback and ebook of the journal, at no cost to you. Ticked, the paper stays online-only."},
]
CATEGORY_IDS = {c["id"] for c in CATEGORIES}
PROMOTION_IDS = {c["id"] for c in CATEGORIES if c["group"] == "promotion"}

CHOICES = {"approve": "approved", "hold": "hold", "withdraw": "withdrawn"}
NOTE_MIN = 20      # characters, for hold / withdraw
NOTE_MAX = 1500    # characters, all notes; over the limit is refused, never cut


class Locked(RuntimeError):
    """The DOI is registered; the link can no longer change anything."""


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(t: _dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _display(t: _dt.datetime) -> str:
    return t.strftime("%B %-d, %Y")


def _state(sub_dir: Path) -> dict:
    p = Path(sub_dir) / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _update_state(sub_dir: Path, **fields) -> dict:
    p = Path(sub_dir) / "state.json"
    data = _state(sub_dir)
    data.update(fields)
    p.write_text(json.dumps(data, indent=2))
    return data


def _load(sub_dir: Path) -> Optional[dict]:
    p = Path(sub_dir) / FILE
    return json.loads(p.read_text()) if p.exists() else None


def _save(sub_dir: Path, rec: dict) -> None:
    (Path(sub_dir) / FILE).write_text(json.dumps(rec, indent=2) + "\n")


def is_test(sub_id: str) -> bool:
    return sub_id.startswith("ICSAC-SUB-TEST-")


# ── issue (called by apply_decision at accept) ────────────────────────────────

def issue(sub_dir: Path, *, days: Optional[int] = None, force: bool = False) -> dict:
    """Create the author's link and the window. Returns url, deadline strings."""
    sub_dir = Path(sub_dir)
    prior = _load(sub_dir)
    if prior and (prior.get("responses") or prior.get("status") != "pending") and not force:
        # The author has already answered (or held, or withdrawn); a second accept
        # must not silently reset that (audit 2026-09-28 item 6).
        raise RuntimeError(f"{sub_dir.name}: an author response is already on record "
                           f"(status {prior.get('status')}); refusing to re-issue the window")
    days = int(days or getattr(config, "OBJECTION_WINDOW_DAYS", 7))
    token = secrets.token_urlsafe(24)
    deadline = _now() + _dt.timedelta(days=days)
    rec = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "issued_at": _iso(_now()),
        "deadline": _iso(deadline),
        "status": "pending",
        "responses": [],
        "window_closed_pinged": False,
    }
    _save(sub_dir, rec)
    _update_state(sub_dir, author_approval_status="pending",
                  author_window_deadline=rec["deadline"])
    t = f"{sub_dir.name}.{token}"
    base = getattr(config, "SITE_BASE_URL", "https://icsacinstitute.org").rstrip("/")
    return {"token": t, "url": f"{base}/approve/?t={t}",
            "deadline": rec["deadline"], "deadline_display": _display(deadline)}


# ── resolve a link ────────────────────────────────────────────────────────────

def resolve(t: str) -> tuple[Path, dict]:
    """'<sub_id>.<token>' -> (sub_dir, record). ValueError on anything wrong;
    callers answer 404 without saying which part failed."""
    sub_id, _, token = (t or "").partition(".")
    if not SUB_ID_RE.match(sub_id) or len(token) < 20:
        raise ValueError("bad link")
    sub_dir = (TEST_SUBMISSIONS_ROOT if is_test(sub_id) else SUBMISSIONS_ROOT) / sub_id
    rec = _load(sub_dir)
    if not rec:
        raise ValueError("no approval on record")
    if not secrets.compare_digest(rec["token_sha256"], hashlib.sha256(token.encode()).hexdigest()):
        raise ValueError("bad token")
    return sub_dir, rec


def public_view(sub_dir: Path, rec: dict) -> dict:
    """What the page shows. No emails, no paths, no internals."""
    sub = json.loads((Path(sub_dir) / "submission.json").read_text())
    st = _state(sub_dir)
    author = ""
    try:
        from crossref_deposit import display_name
        creators = sub.get("creators") or []
        if creators:
            c0 = creators[0] if isinstance(creators[0], dict) else {"name": str(creators[0])}
            author = display_name(c0.get("name", ""))
            if len(creators) > 1:
                author += " et al."
    except Exception:
        author = (sub.get("form") or {}).get("name", "")
    deadline = _dt.datetime.fromisoformat(rec["deadline"].replace("Z", "+00:00"))
    last = rec["responses"][-1] if rec.get("responses") else {}
    return {
        "sub_id": Path(sub_dir).name,
        "test_mode": is_test(Path(sub_dir).name),
        # The page compares the signed-in ORCID with this before it enables the
        # buttons; test records carry a tier token, not an ORCID, so they send "".
        "submitter_orcid": "" if is_test(Path(sub_dir).name) else str((sub.get("form") or {}).get("orcid") or ""),
        "title": sub.get("title") or "",
        "author_display": author,
        "reserved_doi": st.get("crossref_doi") or "",
        "registered": bool(st.get("crossref_registered_at")),
        "deadline": rec["deadline"],
        "deadline_display": _display(deadline),
        "window_open": _now() < deadline,
        "status": rec.get("status", "pending"),
        "exclusions": list(last.get("exclusions") or []),
        "quote_ok": bool(last.get("quote_ok")),
        "responded_at": last.get("at"),
        "categories": CATEGORIES,
        "note_min": NOTE_MIN,
        "note_max": NOTE_MAX,
    }


# ── record a decision ─────────────────────────────────────────────────────────

def record(sub_dir: Path, rec: dict, choice: str, note: str = "",
           exclusions: Optional[list] = None, *, quote_ok: Optional[bool] = None,
           audit: Optional[Callable[[dict], None]] = None) -> dict:
    """quote_ok: the author ticked "you may quote my notes" (2026-09-28). Off unless
    ticked; recorded with the response and mirrored to state.author_quote_ok. Nothing
    author-facing ever asks for a reply: choices are buttons and links."""
    sub_dir = Path(sub_dir)
    sub_id = sub_dir.name
    if choice not in CHOICES:
        raise ValueError(f"choice must be one of {sorted(CHOICES)}")
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise ValueError(f"note is {len(note)} characters; the maximum is {NOTE_MAX}. "
                         f"Please shorten it (nothing was recorded).")
    if choice in ("hold", "withdraw") and len(note) < NOTE_MIN:
        raise ValueError(f"a note of at least {NOTE_MIN} characters is required to hold or withdraw")
    excl = sorted({str(x) for x in (exclusions or [])})
    bad = [x for x in excl if x not in CATEGORY_IDS]
    if bad:
        raise ValueError(f"unknown exclusion(s): {bad}")
    if choice != "approve" and excl:
        excl = []  # exclusions only mean something on an approval
    st = _state(sub_dir)
    if st.get("crossref_registered_at"):
        raise Locked("the DOI is already registered; write to help@icsacinstitute.org for a correction")
    entry = {"at": _iso(_now()), "choice": choice, "exclusions": excl, "note": note,
             "quote_ok": bool(quote_ok) and choice == "approve"}
    rec.setdefault("responses", []).append(entry)
    rec["status"] = CHOICES[choice]
    _save(sub_dir, rec)
    fields = {
        "author_approval_status": rec["status"],
        "author_exclusions": excl,
        "promotion_opt_out": bool(set(excl) & PROMOTION_IDS),
        "persistence_opt_out": "persistence" in excl,
        "withdrawn_by_author": choice == "withdraw",
        "author_quote_ok": entry["quote_ok"],
    }
    if choice in ("hold", "withdraw"):
        fields["author_hold_note"] = note
    _update_state(sub_dir, **fields)
    if audit:
        try:
            audit({"sub_id": sub_id, "event": "author_response", "choice": choice,
                   "exclusions": excl, "quote_ok": entry["quote_ok"], "note": note[:300], "by": "author"})
        except Exception:
            pass
    if not is_test(sub_id):
        _ping(_curator_text(sub_id, choice, excl, note, entry["quote_ok"]))
    return {"sub_id": sub_id, "status": rec["status"], "exclusions": excl, "recorded_at": entry["at"]}


def _labels(excl: list) -> str:
    by_id = {c["id"]: (c.get("short") or c["label"]) for c in CATEGORIES}
    return ", ".join(by_id.get(x, x) for x in excl) if excl else "none"


def _curator_text(sub_id: str, choice: str, excl: list, note: str, quote_ok: bool = False) -> str:
    if choice == "approve":
        body = (f"Author APPROVED publication. Exclusions: {_labels(excl)}. "
                f"Quote permission: {'yes' if quote_ok else 'no'}."
                + (f"\nNote: {note[:400]}" if note else "")
                + f"\nNext: intake/register-doi.sh {sub_id} --live")
    elif choice == "hold":
        body = (f"Author asked to HOLD: {note[:400]}\n"
                f"register --live is refused until they approve (or you re-run decide/accept after fixing).")
    else:
        body = f"Author WITHDREW the paper: {note[:400]}\nregister --live is refused. Nothing to publish."
    return f"AUTHOR RESPONSE — {sub_id}\n{body}"


def _ping(msg: str) -> bool:
    try:
        import notify
        notify.send_to_curator(msg, parse_mode=None)
        return True
    except Exception as exc:
        print(f"  author_approval: curator ping failed: {exc}", file=sys.stderr)
        return False


# ── the window (called by the batch tick) ────────────────────────────────────

def check_windows(*, now: Optional[_dt.datetime] = None) -> int:
    """Ping the curation team ONCE per accepted paper whose window closed in silence."""
    now = now or _now()
    pinged = 0
    if not SUBMISSIONS_ROOT.is_dir():
        return 0
    for sub_dir in sorted(SUBMISSIONS_ROOT.iterdir()):
        if not sub_dir.is_dir() or sub_dir.name in ("queue", "test"):
            continue
        rec = _load(sub_dir)
        if not rec or rec.get("status") != "pending" or rec.get("window_closed_pinged"):
            continue
        st = _state(sub_dir)
        if st.get("decision") != "accept" or st.get("crossref_registered_at"):
            continue
        try:
            deadline = _dt.datetime.fromisoformat(rec["deadline"].replace("Z", "+00:00"))
        except Exception as exc:
            print(f"  author_approval: {sub_dir.name} has an unreadable deadline ({exc}); skipped",
                  file=sys.stderr)
            continue
        if now < deadline:
            continue
        # Marked as pinged only when the ping went out; a failed transport tries
        # again next tick instead of going silent (audit 2026-09-28 item 21).
        if not _ping(f"OBJECTION WINDOW CLOSED — {sub_dir.name}\n"
                     f"Accepted {str(st.get('completed_at', '?'))[:10]}; the author has not responded by "
                     f"{rec['deadline'][:10]}. Your call: intake/register-doi.sh {sub_dir.name} --live"):
            continue
        rec["window_closed_pinged"] = True
        rec["window_closed_pinged_at"] = _iso(now)
        _save(sub_dir, rec)
        pinged += 1
    return pinged


# ── the gate (read by crossref_deposit.register live) ─────────────────────────

def gate(sub_dir: Path, *, override_window: bool = False,
         now: Optional[_dt.datetime] = None) -> tuple[bool, str]:
    """(ok, reason). Withdrawal and hold always refuse. Approval passes. A
    pending window passes only once closed, or with the explicit override."""
    now = now or _now()
    rec = _load(sub_dir)
    st = _state(sub_dir)
    if st.get("withdrawn_by_author") or (rec or {}).get("status") == "withdrawn":
        return False, "the author withdrew this paper"
    if rec is None:
        # Only papers accepted before the response page existed pass without a
        # record; a newer acceptance with no window is a broken accept, not a
        # grandfathered one (audit 2026-09-28 item 5).
        accepted = str(st.get("completed_at") or "")[:10]
        if accepted and accepted < "2026-09-27":
            return True, f"no author window on record (accepted {accepted}, before the response page)"
        return False, (f"no author window on record for an acceptance dated {accepted or 'unknown'}; "
                       f"re-run intake/decide.sh {sub_dir.name} accept with ICSAC_DECISION_FORCE=1 "
                       f"to issue one")
    status = rec.get("status", "pending")
    if status == "hold":
        return False, f"the author asked to hold: {str(st.get('author_hold_note', ''))[:200]}"
    if status == "approved":
        last = rec["responses"][-1] if rec.get("responses") else {}
        return True, (f"author approved at {last.get('at', '?')}; exclusions: "
                      f"{_labels(last.get('exclusions') or [])}")
    deadline = _dt.datetime.fromisoformat(rec["deadline"].replace("Z", "+00:00"))
    if now >= deadline:
        return True, f"objection window closed {rec['deadline'][:10]} with no response"
    if override_window:
        return True, f"objection window open until {rec['deadline'][:10]}; overridden by operator"
    return False, (f"objection window is open until {rec['deadline'][:10]} and the author has not "
                   f"responded; wait, or pass --override-window")
