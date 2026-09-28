"""Panel retry and the 24-hour scream (2026-09-28, the curator's spec).

A submission the panel could not staff (state `paused_panel_failure`: fewer
than MIN_REVIEWERS slots answered after self-heal) no longer waits for a human
to re-queue it. Every batch tick re-queues it once by dropping its marker back
into queue/, which the worker's path unit picks up. Free-tier providers refresh
daily, so a paper that could not be staffed this morning is often staffable
tonight.

The clock: `panel_wait_started_at` is the first moment the panel failed to
staff the paper (its first pause). Time spent in queue/ before the worker first
picked it up is NOT counted ("not including any queue time the paper sat in").
Once a paper has waited PANEL_SCREAM_HOURS (24) for reviewers, every tick
SCREAMS -- Telegram to the curation team and the pain endpoint -- until it is
staffed or a human steps in. A paper stuck `in_review` for STUCK_HOURS (3) with
no result and no marker means the worker died mid-run: it is re-queued and it
screams at once, because that is a defect, not a quota.

Guards: a marker already in queue/ means the worker is on it or will be, so
nothing is written twice. Papers with a curator decision, or past the panel,
are ignored. Production only from the batch tick; --include-test for rehearsals
(test papers never ping).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Callable, Iterator, Optional

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import config  # noqa: E402

SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
QUEUE_DIR = SUBMISSIONS_ROOT / "queue"
TEST_SUBMISSIONS_ROOT = SUBMISSIONS_ROOT / "test"
TEST_QUEUE_DIR = TEST_SUBMISSIONS_ROOT / "queue"
AUDIT_LOG = Path(getattr(config, "REVIEWS_DIR", _REPO_ROOT / "reviews")) / "audit-log.jsonl"
TEST_AUDIT_LOG = Path(getattr(config, "REVIEWS_DIR", _REPO_ROOT / "reviews")) / "audit-log-test.jsonl"

PANEL_SCREAM_HOURS = float(getattr(config, "PANEL_SCREAM_HOURS", 0)
                           or os.environ.get("ICSAC_PANEL_SCREAM_HOURS", "").strip() or 24)
STUCK_HOURS = float(getattr(config, "PANEL_STUCK_HOURS", 0)
                    or os.environ.get("ICSAC_PANEL_STUCK_HOURS", "").strip() or 3)
# After this many automatic re-queues (twice a day = about five days) the paper
# keeps screaming but is no longer re-run: a paper that cannot be staffed for
# five days needs a human, not a twelfth identical attempt.
MAX_RETRIES = int(getattr(config, "PANEL_MAX_RETRIES", 0)
                  or os.environ.get("ICSAC_PANEL_MAX_RETRIES", "").strip() or 10)
PAUSED = "paused_panel_failure"
IN_REVIEW = "in_review"
_SKIP_DIRS = {"queue", "test", "_outbox"}


# ── helpers ───────────────────────────────────────────────────────────────────

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


# Papers whose wait began before automatic retries existed are LEGACY: announced
# to the curation team ONCE and left parked. A paper paused for months is a
# decision for a human, not a re-run. Set PANEL_RETRY_SINCE to move the line.
RETRY_SINCE = _parse(getattr(config, "PANEL_RETRY_SINCE", "")
                     or os.environ.get("ICSAC_PANEL_RETRY_SINCE", "").strip()
                     or "2026-09-28T00:00:00Z")


def is_test(sub_id: str) -> bool:
    return sub_id.startswith("ICSAC-SUB-TEST-")


def _state(sub_dir: Path) -> dict:
    p = Path(sub_dir) / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _update_state(sub_dir: Path, **fields) -> dict:
    p = Path(sub_dir) / "state.json"
    data = _state(sub_dir)
    data.update(fields)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)
    return data


def _audit(event: dict, *, test_mode: bool) -> None:
    target = TEST_AUDIT_LOG if test_mode else AUDIT_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": _iso(_now()), **event}
        if test_mode:
            payload.setdefault("test", True)
        with target.open("a") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception as exc:
        print(f"  panel_retry: audit write failed: {exc}", file=sys.stderr)


def _telegram(msg: str) -> None:
    try:
        import notify
        notify.send_to_curator(msg, parse_mode=None)
    except Exception as exc:
        print(f"  panel_retry: curator ping failed: {exc}", file=sys.stderr)


def _pain(title: str, message: str) -> None:
    url = getattr(config, "NTFY_PAIN_URL", "")
    if not url:
        return
    try:
        import urllib.request
        req = urllib.request.Request(url, data=message.encode())
        req.add_header("Title", title)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── classification ────────────────────────────────────────────────────────────

def classify(state: dict, now: Optional[_dt.datetime] = None) -> tuple[str, str]:
    """paused | stuck | running | done | other, with a detail string."""
    now = now or _now()
    if state.get("decision"):
        return "done", f"decision={state['decision']}"
    st = str(state.get("state") or "")
    if st == PAUSED:
        return "paused", str(state.get("panel_paused_at") or "")
    if st == IN_REVIEW:
        started = _parse(state.get("review_started_at"))
        if started and (now - started) >= _dt.timedelta(hours=STUCK_HOURS):
            return "stuck", f"in_review since {state.get('review_started_at')}"
        return "running", str(state.get("review_started_at") or "")
    if st in ("awaiting_decision", "completed", "completed_email_failed", "published") or st.startswith("rejected"):
        return "done", st
    return "other", st


def _iter_subs(include_test: bool) -> Iterator[tuple[Path, Path]]:
    """(sub_dir, queue_dir) for every submission with a state file."""
    roots = [(SUBMISSIONS_ROOT, QUEUE_DIR)] + ([(TEST_SUBMISSIONS_ROOT, TEST_QUEUE_DIR)] if include_test else [])
    for root, q in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in _SKIP_DIRS:
                continue
            if (d / "state.json").exists():
                yield d, q


def wait_started(state: dict, kind: str) -> Optional[_dt.datetime]:
    """When the paper started waiting for reviewers: the recorded start, else
    the first pause, else (stuck) the review start. Never the receipt time."""
    return (_parse(state.get("panel_wait_started_at"))
            or _parse(state.get("panel_paused_at"))
            or (_parse(state.get("review_started_at")) if kind == "stuck" else None))


# ── the tick ──────────────────────────────────────────────────────────────────

def _requeue(sub_dir: Path, queue_dir: Path, *, kind: str, now: _dt.datetime,
             test_mode: bool, dry_run: bool, log: Callable[[str], None]) -> bool:
    sub_id = sub_dir.name
    marker = queue_dir / sub_id
    if marker.exists():
        log(f"  panel: {sub_id} already in the queue; leaving it")
        return False
    state = _state(sub_dir)
    if int(state.get("panel_retry_count") or 0) >= MAX_RETRIES:
        log(f"  panel: {sub_id} has been re-queued {state.get('panel_retry_count')} times; not again (needs a human)")
        return False
    if dry_run:
        log(f"  panel: {sub_id} ({kind}) would be re-queued")
        return True
    started = wait_started(state, kind) or now
    queue_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"sub_id": sub_id, "requeued_by": "panel_retry", "at": _iso(now)}) + "\n")
    _update_state(sub_dir,
                  panel_retry_count=int(state.get("panel_retry_count") or 0) + 1,
                  panel_retried_at=_iso(now),
                  panel_wait_started_at=_iso(started))
    _audit({"sub_id": sub_id, "event": "panel_requeued", "reason": kind, "by": "batch-tick",
            "retry": int(state.get("panel_retry_count") or 0) + 1}, test_mode=test_mode)
    log(f"  panel: {sub_id} ({kind}) re-queued, retry {int(state.get('panel_retry_count') or 0) + 1}")
    return True


def _scream(sub_dir: Path, *, kind: str, waited: _dt.timedelta, now: _dt.datetime,
            test_mode: bool, dry_run: bool, log: Callable[[str], None]) -> None:
    sub_id = sub_dir.name
    state = _state(sub_dir)
    hours = waited.total_seconds() / 3600
    retries = int(state.get("panel_retry_count") or 0)
    if kind == "stuck":
        why = (f"the worker has been 'in_review' since {state.get('review_started_at')} with no result: "
               f"it died mid-run")
    else:
        why = "no reviewers available: every slot chain failed after self-heal, re-queued each tick"
    capped = retries >= MAX_RETRIES
    msg = (f"PANEL SCREAM: {sub_id} has waited {hours:.0f} h for reviewers ({why}). "
           f"Re-queued {retries} time(s)" + (", the cap; it will not be re-run again on its own. " if capped else ". ")
           + f"Nothing will change until a model source answers or you step in: "
           f"credits, keys, or `python intake/submission_worker.py {sub_id}` by hand after fixing the cause.")
    log(f"  panel: SCREAM {sub_id} {hours:.0f} h ({kind})")
    if dry_run:
        return
    _update_state(sub_dir, panel_scream_count=int(state.get("panel_scream_count") or 0) + 1,
                  panel_last_scream_at=_iso(now))
    _audit({"sub_id": sub_id, "event": "panel_scream", "hours": round(hours, 1), "kind": kind,
            "retries": retries, "by": "batch-tick"}, test_mode=test_mode)
    if not test_mode:
        _telegram(msg)
        _pain("ICSAC panel: paper waiting for reviewers", msg)


def tick(now: Optional[_dt.datetime] = None, *, include_test: bool = False,
         dry_run: bool = False, log: Callable[[str], None] = print) -> dict:
    now = now or _now()
    summary = {"checked": 0, "requeued": 0, "screamed": 0, "stuck": 0, "legacy": 0, "errors": 0, "transitions": []}
    for sub_dir, queue_dir in _iter_subs(include_test):
        sub_id = sub_dir.name
        try:
            state = _state(sub_dir)
            kind, _detail = classify(state, now)
            if kind not in ("paused", "stuck"):
                continue
            summary["checked"] += 1
            test_mode = is_test(sub_id) or bool(state.get("test_mode"))
            started0 = wait_started(state, kind)
            if started0 and RETRY_SINCE and started0 < RETRY_SINCE:
                summary["legacy"] += 1
                if not state.get("panel_legacy_notified_at") and not dry_run:
                    title = ""
                    try:
                        title = json.loads((sub_dir / "submission.json").read_text()).get("title") or ""
                    except Exception:
                        pass
                    msg = (f"PANEL: {sub_id} ({title[:80] or 'untitled'}) has been paused since "
                           f"{started0:%Y-%m-%d}, before automatic retries existed. Not re-run on its own. "
                           f"Your call: `python intake/submission_worker.py {sub_id}` to re-run it, or leave it.")
                    _update_state(sub_dir, panel_legacy_notified_at=_iso(now))
                    _audit({"sub_id": sub_id, "event": "panel_legacy_notice", "paused_since": _iso(started0),
                            "by": "batch-tick"}, test_mode=test_mode)
                    if not test_mode:
                        _telegram(msg)
                log(f"  panel: {sub_id} paused since {started0:%Y-%m-%d} (legacy): left parked")
                continue
            if kind == "stuck":
                summary["stuck"] += 1
            if _requeue(sub_dir, queue_dir, kind=kind, now=now, test_mode=test_mode, dry_run=dry_run, log=log):
                summary["requeued"] += 1
                summary["transitions"].append(sub_id)
            started = wait_started(_state(sub_dir), kind) or now
            waited = now - started
            if kind == "stuck" or waited >= _dt.timedelta(hours=PANEL_SCREAM_HOURS):
                _scream(sub_dir, kind=kind, waited=waited, now=now, test_mode=test_mode, dry_run=dry_run, log=log)
                summary["screamed"] += 1
        except Exception as exc:
            log(f"  panel: {sub_id} failed: {type(exc).__name__}: {exc}")
            summary["errors"] += 1
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="panel_retry.py",
                                 description="Re-queue papers the panel could not staff; scream after 24 h.")
    ap.add_argument("--include-test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="pretend the clock reads this ISO-8601 UTC instant")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    now = _parse(a.now) if a.now else _now()
    if a.now and not now:
        print(f"bad --now {a.now!r}", file=sys.stderr)
        return 2
    if a.status:
        for d, _q in _iter_subs(a.include_test):
            st = _state(d)
            kind, detail = classify(st, now)
            started = wait_started(st, kind)
            waited = f"{(now - started).total_seconds() / 3600:.1f} h" if started and kind in ("paused", "stuck") else "-"
            legacy = " (legacy: left parked)" if started and RETRY_SINCE and started < RETRY_SINCE and kind in ("paused", "stuck") else ""
            print(f"{d.name:32s} {kind:8s} waited={waited:8s} retries={st.get('panel_retry_count', 0)} {detail}{legacy}")
        return 0
    s = tick(now, include_test=a.include_test, dry_run=a.dry_run)
    print(json.dumps(s, indent=2))
    return 0 if not s["errors"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
