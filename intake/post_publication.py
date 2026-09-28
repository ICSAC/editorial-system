"""Post-publication follow-up for accepted, registered papers (2026-09-28).

After a paper's DOI is registered (crossref_deposit.register --live) exactly ONE
follow-up may go to its author, FOLLOWUP_DAYS (default 14) later: an invitation
to join the Institute as a Reviewer. It is DRAFTED to Gmail for the curation team
to read and send; this code never sends mail. There is no second follow-up and no
list behind it: the author is asked once, about this paper.

Stop conditions are read from the record and applied mechanically, never
editorially. The author's own response record (author_approval.json) is the
source of truth; state.json is only its mirror:
  * the author was never shown the exclusion choices (accepted before the
    response page existed, no author_approval.json)   -> no follow-up
  * the author excluded `newsletter`                   -> no follow-up
    (read as "no unsolicited mail beyond the paper's own emails")
  * the author asked to hold, or withdrew              -> no follow-up
  * the author already holds an ICSAC credential or is a listed supporter
    (ORCID match against the website registries)      -> no follow-up
  * the registries cannot be read completely          -> no follow-up, and the
    curation team is told once (asking by hand is theirs)
  * no curator accept, no registered DOI, no email     -> nothing / no follow-up
  * a follow-up was already drafted, or attempted      -> never again
Every outcome is written to state.json (followup_drafted_at, or
followup_skipped_at + followup_skip_reason) and to the audit log, so the batch
tick can run any number of times. A reservation (followup_draft_started_at) is
written BEFORE the Gmail append; a reservation whose outcome was never recorded
(crash, power loss) closes the record instead of drafting blind, and the
curation team is told to look in Drafts. Per-paper work is serialised with a
file lock, and state.json is replaced atomically.

Test tiers: T2 -> outbox .eml, no pings. T3 (or any test record without a
usable tier) -> Gmail draft with the `[T3 TEST] ` subject prefix, pings to
TELEGRAM_TEST_CHAT_ID or suppressed. Test submissions are only visited with
include_test=True; the batch tick passes False, exactly like
author_approval.check_windows.

wants_stay_involved() applies the same rules to the one "ways to stay involved"
paragraph in the published notice, and fails CLOSED: anything unreadable means
the paragraph is left out.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Iterator, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import config  # noqa: E402

SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
TEST_SUBMISSIONS_ROOT = SUBMISSIONS_ROOT / "test"
AUDIT_LOG = Path(getattr(config, "REVIEWS_DIR", _REPO_ROOT / "reviews")) / "audit-log.jsonl"
TEST_AUDIT_LOG = Path(getattr(config, "REVIEWS_DIR", _REPO_ROOT / "reviews")) / "audit-log-test.jsonl"

FOLLOWUP_DAYS = int(getattr(config, "FOLLOWUP_DAYS", 0)
                    or os.environ.get("ICSAC_FOLLOWUP_DAYS", "").strip() or 14)
MAX_DRAFT_ATTEMPTS = 3            # returned failures before the record is closed and the curator is told once
RESERVATION_STALE = _dt.timedelta(minutes=30)   # older than this with no outcome = a lost draft attempt
WEBSITE_REPO = (getattr(config, "ICSAC_WEBSITE_REPO", "")
                or os.environ.get("ICSAC_WEBSITE_REPO", ""))
REGISTRY_FILES = ("src/data/members.yaml", "src/data/supporters.yaml")
LIVE_STATUSES = {"active", "grace_period", "promoted"}   # revoked / expired records do not count
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
APPROVAL_FILE = "author_approval.json"
_SKIP_DIRS = {"queue", "test", "_outbox"}


class RegistryUnavailable(RuntimeError):
    """The website registries could not be read completely; the answer is unknown."""


# ── small helpers ─────────────────────────────────────────────────────────────

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(t: _dt.datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso: Optional[str]) -> Optional[_dt.datetime]:
    if not iso:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def is_test(sub_id: str) -> bool:
    return sub_id.startswith("ICSAC-SUB-TEST-")


def _state(sub_dir: Path) -> dict:
    p = Path(sub_dir) / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _update_state(sub_dir: Path, **fields) -> dict:
    """Merge fields into state.json and replace the file atomically (a crash
    mid-write can never leave a truncated record behind)."""
    p = Path(sub_dir) / "state.json"
    data = _state(sub_dir)
    data.update(fields)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)
    return data


def _submission(sub_dir: Path) -> dict:
    return json.loads((Path(sub_dir) / "submission.json").read_text())


def _audit(event: dict, *, test_mode: bool) -> None:
    target = TEST_AUDIT_LOG if test_mode else AUDIT_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": _iso(_now()), **event}
        if test_mode:
            payload.setdefault("test", True)
        with target.open("a") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception as exc:  # the audit line is evidence, not a gate
        print(f"  followup: audit write failed: {exc}", file=sys.stderr)


def _routing(test_mode: bool, tier: int) -> dict:
    """Curator-ping routing, the same table apply_decision uses (prod: default
    chat; T3: the test chat or full suppression; T2: suppressed). Imported
    lazily so the batch tick does not load the decision module; if that import
    fails, a test run is suppressed and a production run uses the default chat."""
    if not test_mode:
        return {}
    try:
        from .apply_decision import _curator_routing
        return _curator_routing(test_mode, tier)
    except Exception:
        return {"chat_override": "__suppress__", "ntfy": False}


def _ping(msg: str, *, test_mode: bool = False, tier: int = 1) -> None:
    try:
        import notify
        notify.send_to_curator(msg, parse_mode=None, **_routing(test_mode, tier))
    except Exception as exc:
        print(f"  followup: curator ping failed: {exc}", file=sys.stderr)


def _tier(submission: dict, test_mode: bool) -> int:
    """Delivery tier. A test record is never treated as production: T2 stays T2,
    anything else under test goes the T3 way (prefixed draft, test-chat pings)."""
    if not test_mode:
        return 1
    try:
        t = int(submission.get("tier") or 0)
    except (TypeError, ValueError):
        t = 0
    return 2 if t == 2 else 3


# ── the author's own response (source of truth), with the state mirror behind it

def author_response(sub_dir: Path, state: Optional[dict] = None) -> dict:
    """{offered, status, exclusions}. `offered` is False when the author was
    never given the response page (no author_approval.json). The latest response
    in that record wins over state.json's mirror, which is written a moment later."""
    state = state if state is not None else _state(sub_dir)
    p = Path(sub_dir) / APPROVAL_FILE
    if not p.exists():
        return {"offered": False, "status": str(state.get("author_approval_status") or ""),
                "exclusions": list(state.get("author_exclusions") or [])}
    rec = json.loads(p.read_text())
    last = rec["responses"][-1] if rec.get("responses") else {}
    return {"offered": True, "status": str(rec.get("status") or "pending"),
            "exclusions": sorted({str(x) for x in (last.get("exclusions") or [])})}


# ── the author's ORCID against the website registries ────────────────────────

def bare_orcid(s: Optional[str]) -> str:
    s = (s or "").strip()
    s = re.sub(r"^https?://(?:www\.)?orcid\.org/", "", s, flags=re.I)
    m = ORCID_RE.fullmatch(s.upper())
    return m.group(0) if m else ""


def author_orcid(submission: dict) -> str:
    """The iD intake verified by OAuth when there is one, else what the form said."""
    auth = submission.get("auth") or {}
    if auth.get("verified") and bare_orcid(auth.get("orcid")):
        return bare_orcid(auth.get("orcid"))
    return bare_orcid((submission.get("form") or {}).get("orcid"))


def _yaml_hits(orcid: str, path: Path) -> list[str]:
    """Live registry records carrying this ORCID (status-aware; needs PyYAML).
    A file whose collections are not the shapes the site schema defines raises
    RegistryUnavailable: an unrecognised file is an unknown answer, not 'no'."""
    import yaml  # ImportError handled by the caller
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise RegistryUnavailable(f"{path.name}: top level is not a mapping")
    hits: list[str] = []
    if "members" in data:
        members = data.get("members") or {}
        if not isinstance(members, dict):
            raise RegistryUnavailable(f"{path.name}: `members` is not a mapping")
        for mid, rec in members.items():
            if isinstance(rec, dict) and bare_orcid(str(rec.get("orcid") or "")) == orcid \
                    and str(rec.get("status") or "active") in LIVE_STATUSES:
                hits.append(f"members:{mid}")
    if "supporters" in data:
        supporters = data.get("supporters") or []
        if not isinstance(supporters, list):
            raise RegistryUnavailable(f"{path.name}: `supporters` is not a list")
        for rec in supporters:
            if isinstance(rec, dict) and bare_orcid(str(rec.get("orcid") or "")) == orcid \
                    and str(rec.get("status") or "active") in LIVE_STATUSES:
                hits.append(f"supporters:{rec.get('tier', 'supporter')}")
    if "members" not in data and "supporters" not in data:
        raise RegistryUnavailable(f"{path.name}: neither `members` nor `supporters` present")
    return hits


def already_affiliated(orcid: str, *, repo: Optional[str] = None) -> tuple[Optional[str], str]:
    """(hit, note). hit = 'members:ICSAC-00012' or 'supporters:sponsor' when the
    ORCID sits on a live registry record, else None once BOTH files were read.
    note explains a degraded-but-complete lookup ('' when clean). Raises
    RegistryUnavailable when the answer is unknown (no repo configured or
    present, a file missing or unreadable, an unrecognised shape) so callers
    err toward NOT asking. Without PyYAML the check is a presence scan, which is
    status-blind and errs the same way."""
    orcid = bare_orcid(orcid)
    if not orcid:
        return None, "no ORCID on the submission"
    repo = repo if repo is not None else WEBSITE_REPO
    if not repo:
        raise RegistryUnavailable("ICSAC_WEBSITE_REPO is not configured on this host")
    root = Path(repo)
    if not root.is_dir():
        raise RegistryUnavailable(f"website tree not found at {root}")
    notes: list[str] = []
    for rel in REGISTRY_FILES:
        path = root / rel
        if not path.exists():
            raise RegistryUnavailable(f"{rel} missing under {root}")
        try:
            hits = _yaml_hits(orcid, path)
        except ImportError:
            try:
                hits = [f"{rel} (presence only)"] if orcid in path.read_text().upper() else []
            except Exception as exc:
                raise RegistryUnavailable(f"{rel} unreadable: {type(exc).__name__}") from exc
            notes.append("PyYAML missing; presence scan used")
        except RegistryUnavailable:
            raise
        except Exception as exc:
            raise RegistryUnavailable(f"{rel} unreadable: {type(exc).__name__}") from exc
        if hits:
            return hits[0], "; ".join(notes)
    return None, "; ".join(notes)


# ── the state machine ─────────────────────────────────────────────────────────

def phase(state: dict, now: Optional[_dt.datetime] = None) -> tuple[str, str]:
    """One of: not_published | waiting | due | drafted | skipped, with a detail."""
    now = now or _now()
    if state.get("followup_drafted_at"):
        return "drafted", str(state["followup_drafted_at"])
    if state.get("followup_skipped_at"):
        return "skipped", str(state.get("followup_skip_reason") or "")
    registered = _parse(state.get("crossref_registered_at"))
    if not registered:
        return "not_published", "no crossref_registered_at"
    due_at = registered + _dt.timedelta(days=FOLLOWUP_DAYS)
    if now < due_at:
        return "waiting", _iso(due_at)
    return "due", _iso(due_at)


def stop_reason(sub_dir: Path, submission: dict, state: dict) -> tuple[Optional[str], bool]:
    """(reason, tell_curator). reason is why a DUE follow-up must not be drafted,
    or None. tell_curator is True only for the one reason a human may want to
    act on (the registries could not be read). Mechanical: it reads the record
    and the registries, it does not weigh anything."""
    if state.get("decision") != "accept":
        return f"no curator accept on record (decision={state.get('decision')!r})", False
    resp = author_response(sub_dir, state)
    if not resp["offered"]:
        return "the author was never offered the exclusion choices (accepted before 2026-09-27)", False
    if state.get("withdrawn_by_author") or resp["status"] == "withdrawn":
        return "the author withdrew the paper", False
    if resp["status"] == "hold":
        return "the author asked to hold", False
    if "newsletter" in resp["exclusions"] or "newsletter" in (state.get("author_exclusions") or []):
        return "the author excluded email newsletters and announcements", False
    try:
        hit, _note = already_affiliated(author_orcid(submission))
    except RegistryUnavailable as exc:
        return f"the website registries could not be checked ({exc}); not asking blind", True
    if hit:
        return f"the author is already on the registry ({hit})", False
    if "@" not in str((submission.get("form") or {}).get("email") or ""):
        return "no author email on record", False
    return None, False


def wants_stay_involved(sub_dir: Path, submission: Optional[dict] = None,
                        state: Optional[dict] = None) -> bool:
    """For the published notice: render the 'ways to stay involved' paragraph
    only for an author who was offered the choices, did not exclude newsletters,
    did not hold or withdraw, and is not already on a registry. FAILS CLOSED:
    anything unreadable or unknown means the paragraph is left out."""
    try:
        state = state if state is not None else _state(sub_dir)
        submission = submission or _submission(sub_dir)
        resp = author_response(sub_dir, state)
        if not resp["offered"]:
            return False
        if resp["status"] in ("hold", "withdrawn") or state.get("withdrawn_by_author"):
            return False
        if "newsletter" in resp["exclusions"] or "newsletter" in (state.get("author_exclusions") or []):
            return False
        hit, _ = already_affiliated(author_orcid(submission))
        return hit is None
    except Exception:
        return False


def _iter_subs(include_test: bool) -> Iterator[Path]:
    roots = [SUBMISSIONS_ROOT] + ([TEST_SUBMISSIONS_ROOT] if include_test else [])
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in _SKIP_DIRS:
                continue
            if (d / "state.json").exists() and (d / "submission.json").exists():
                yield d


def due_followups(now: Optional[_dt.datetime] = None, *, include_test: bool = False) -> list[Path]:
    now = now or _now()
    out = []
    for d in _iter_subs(include_test):
        try:
            if phase(_state(d), now)[0] == "due":
                out.append(d)
        except Exception:
            continue
    return out


def _draft(sub_dir: Path, submission: dict, state: dict, *, tier: int,
           log: Callable[[str], None]) -> tuple[bool, str]:
    from . import notify_author
    form = submission.get("form") or {}
    registered = _parse(state.get("crossref_registered_at"))
    return notify_author.send_followup(
        to=str(form.get("email") or ""), sub_id=sub_dir.name,
        title=submission.get("title") or "",
        author_name=str(form.get("name") or "Author"),
        published_date=registered.strftime("%B %-d, %Y") if registered else "",
        tier=tier,
    )


def _close(sub_dir: Path, sub_id: str, now: _dt.datetime, reason: str, *,
           test_mode: bool, tier: int, tell: bool, log) -> None:
    """Record a permanent skip: state first, audit, then (optionally) one ping."""
    _update_state(sub_dir, followup_skipped_at=_iso(now), followup_skip_reason=reason)
    _audit({"sub_id": sub_id, "event": "followup_skipped", "reason": reason,
            "by": "batch-tick"}, test_mode=test_mode)
    log(f"  followup: {sub_id} skipped: {reason}")
    if tell:
        _ping(f"Follow-up NOT drafted for {sub_id}: {reason}\n"
              f"Nothing more will be tried; ask by hand if you want to.",
              test_mode=test_mode, tier=tier)


def _process(sub_dir: Path, now: _dt.datetime, *, dry_run: bool, log,
             summary: dict) -> None:
    """One DUE paper, under its own lock. Every outcome is recorded before any ping."""
    sub_id = sub_dir.name
    state = _state(sub_dir)
    if phase(state, now)[0] != "due":      # re-read under the lock: another run may have finished it
        return
    submission = _submission(sub_dir)
    test_mode = bool(submission.get("test_mode")) or is_test(sub_id)
    tier = _tier(submission, test_mode)

    started = _parse(state.get("followup_draft_started_at"))
    if started:
        # A reservation with no recorded outcome: the last run died between the
        # Gmail append and the state write, or is still running. Never draft blind,
        # and say so before any other reason can hide it.
        if now - started < RESERVATION_STALE:
            log(f"  followup: {sub_id} draft in progress since {state['followup_draft_started_at']}; leaving it")
            return
        reason = (f"a draft was attempted at {state['followup_draft_started_at']} and its outcome was "
                  f"never recorded; look in Gmail Drafts before asking by hand")
        if not dry_run:
            _close(sub_dir, sub_id, now, reason, test_mode=test_mode, tier=tier, tell=True, log=log)
        summary["skipped"] += 1
        return

    reason, tell = stop_reason(sub_dir, submission, state)
    if reason:
        if dry_run:
            log(f"  followup: {sub_id} would be skipped: {reason}")
        else:
            _close(sub_dir, sub_id, now, reason, test_mode=test_mode, tier=tier, tell=tell, log=log)
        summary["skipped"] += 1
        return

    if dry_run:
        log(f"  followup: {sub_id} due (dry run: would draft the reviewer invitation)")
        summary["transitions"].append(sub_id)
        return

    _update_state(sub_dir, followup_draft_started_at=_iso(now))     # the reservation
    try:
        ok, info = _draft(sub_dir, submission, state, tier=tier, log=log)
    except Exception as exc:                                        # render / import / transport crash
        ok, info = False, f"{type(exc).__name__}: {exc}"
    if ok:
        _update_state(sub_dir, followup_drafted_at=_iso(now))
        _audit({"sub_id": sub_id, "event": "followup_drafted", "kind": "reviewer_invitation",
                "by": "batch-tick", "delivery": str(info)[:120]}, test_mode=test_mode)
        log(f"  followup: {sub_id} drafted ({info})")
        summary["drafted"] += 1
        summary["transitions"].append(sub_id)
        to = str((submission.get("form") or {}).get("email") or "")
        _ping(f"Follow-up DRAFT ready: {sub_id}\n"
              f"To: {to}\n"
              f"Reviewer invitation, {FOLLOWUP_DAYS} days after DOI registration. "
              f"Open Gmail -> Drafts to read and send, or delete it. Nothing else follows.",
              test_mode=test_mode, tier=tier)
        return

    # A returned failure means nothing was appended (email_send returns False before
    # or instead of the append). Release the reservation and count the attempt.
    attempts = int(state.get("followup_draft_attempts") or 0) + 1
    fields = {"followup_draft_attempts": attempts, "followup_draft_error": str(info)[:300],
              "followup_draft_started_at": None}
    closed = attempts >= MAX_DRAFT_ATTEMPTS
    if closed:
        fields.update(followup_skipped_at=_iso(now),
                      followup_skip_reason=f"draft failed {attempts} times: {str(info)[:120]}")
    _update_state(sub_dir, **fields)
    _audit({"sub_id": sub_id, "event": "followup_draft_failed", "attempt": attempts,
            "reason": str(info)[:300], "by": "batch-tick"}, test_mode=test_mode)
    log(f"  followup: {sub_id} draft failed (attempt {attempts}): {info}")
    summary["errors"] += 1
    if closed:
        _ping(f"Follow-up draft FAILED {attempts} times for {sub_id}: {str(info)[:200]}\n"
              f"Nothing more will be tried; write it by hand if you want it.",
              test_mode=test_mode, tier=tier)


def run_tick(now: Optional[_dt.datetime] = None, *, include_test: bool = False,
             dry_run: bool = False, log: Callable[[str], None] = print) -> dict:
    """Draft (or skip) every DUE follow-up once. Safe to run on every batch tick
    and beside a manual run: each paper is handled under its own file lock, and
    an unexpected error on one paper never stops the others."""
    now = now or _now()
    summary = {"checked": 0, "drafted": 0, "skipped": 0, "errors": 0, "transitions": []}
    for sub_dir in _iter_subs(include_test):
        try:
            if phase(_state(sub_dir), now)[0] != "due":
                continue
        except Exception as exc:
            log(f"  followup: {sub_dir.name} unreadable state: {exc}")
            summary["errors"] += 1
            continue
        summary["checked"] += 1
        lock_path = sub_dir / ".followup.lock"
        try:
            with open(lock_path, "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    _process(sub_dir, now, dry_run=dry_run, log=log, summary=summary)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception as exc:
            log(f"  followup: {sub_dir.name} failed: {type(exc).__name__}: {exc}")
            summary["errors"] += 1
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="post_publication.py",
                                 description="Draft the one post-publication follow-up for every "
                                             "paper whose DOI was registered FOLLOWUP_DAYS ago.")
    ap.add_argument("--include-test", action="store_true",
                    help="also visit ~/icsac-submissions/test/ (T3 drafts get the [T3 TEST] prefix)")
    ap.add_argument("--dry-run", action="store_true", help="report what is due; write nothing")
    ap.add_argument("--now", help="pretend the clock reads this ISO-8601 UTC instant")
    ap.add_argument("--status", action="store_true", help="print each submission's follow-up phase")
    a = ap.parse_args(argv)
    now = _parse(a.now) if a.now else _now()
    if a.now and not now:
        print(f"bad --now {a.now!r}", file=sys.stderr)
        return 2
    if a.status:
        for d in _iter_subs(a.include_test):
            try:
                ph, detail = phase(_state(d), now)
            except Exception as exc:
                ph, detail = "unreadable", str(exc)
            print(f"{d.name:32s} {ph:14s} {detail}")
        return 0
    s = run_tick(now, include_test=a.include_test, dry_run=a.dry_run)
    print(json.dumps(s, indent=2))
    return 0 if not s["errors"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
