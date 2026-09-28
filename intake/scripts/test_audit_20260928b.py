#!/usr/bin/env python3
"""Regression checks for the second audit of 2026-09-28 (the evening sweep).
Offline: no network, no mail, no pings; every write lands in one temporary
directory that is removed at the end, pass or fail. Run from the repo root:
    .venv/bin/python intake/scripts/test_audit_20260928b.py

Covers: a late author response is never overwritten by the window check; a
second response still refuses; the code claim is not taken back by a data
sentence; the redaction gate passes a review of a paper that studies models and
still stops seat talk; accept waits for a panel report.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="icsac-audit-b-"))
try:
    # ── author approval: the window check and a late response ──────────────
    from intake import author_approval as aa
    aa.SUBMISSIONS_ROOT = tmp
    sub = tmp / "ICSAC-SUB-00099"
    sub.mkdir()
    (sub / "state.json").write_text(json.dumps({"state": "completed", "decision": "accept",
                                                "completed_at": "2026-09-01T00:00:00Z"}))
    rec = {"token_sha256": "abc", "issued_at": "2026-09-01T00:00:00Z",
           "deadline": "2026-09-08T00:00:00Z", "status": "pending", "responses": [],
           "window_closed_pinged": False}
    aa._save(sub, rec)
    sent = []

    def ping_while_author_answers(msg: str) -> bool:
        # The author's late approval lands while the curator ping is in flight.
        sent.append(msg)
        if len(sent) == 1:   # record() pings the curator too; answer only once
            aa.record(sub, aa._load(sub), "approve", "", [], orcid="0000-0000-0000-0001")
        return True

    real_ping = aa._ping
    aa._ping = ping_while_author_answers
    try:
        aa.check_windows()
    finally:
        aa._ping = real_ping
    final = aa._load(sub)
    check(final["status"] == "approved" and len(final["responses"]) == 1,
          "a response recorded during the window ping survives the tick")
    check(not final.get("window_closed_pinged"), "the tick does not mark an answered paper as closed in silence")
    try:
        aa.record(sub, aa._load(sub), "withdraw", "x" * 40, [], orcid="0000-0000-0000-0001")
        check(False, "a second response is refused")
    except aa.Locked:
        check(True, "a second response is refused")
    stale = dict(final, token_sha256="other")
    (sub / aa.FILE).write_text(json.dumps(dict(final, status="pending", responses=[])))
    try:
        aa.record(sub, stale, "approve", "", [], orcid="0000-0000-0000-0001")
        check(False, "a response through a re-issued link's old token is refused")
    except aa.Locked:
        check(True, "a response through a re-issued link's old token is refused")
    check(not list(sub.glob("*.tmp")), "no temporary file is left beside the record")

    # ── code claims ─────────────────────────────────────────────────────────
    import code_availability as ca
    check(len(ca.find_claims("The code is available on request. No new data were generated.")) == 1,
          "a data sentence after a code claim does not take the claim back")
    check(ca.find_claims("Data availability\nNot applicable.\n") == [],
          "a heading answered 'Not applicable' is still no claim")
    check(ca.find_claims("Data availability: no new data were generated in this study.") == [],
          "a heading answered 'no new data' is still no claim")
    check(len(ca.find_claims("Code availability: the scripts are available on request.")) >= 1,
          "a heading followed by a real statement is a claim")

    # ── redaction: models studied vs seats named ────────────────────────────
    import redaction
    try:
        redaction.assert_clean("The authors compare the GPT model with two Llama models and a Gemini model.")
        check(True, "a review of a paper that studies models passes the gate")
    except redaction.RedactionLeak:
        check(False, "a review of a paper that studies models passes the gate")
    for leak in ("The Claude-position reviewers agree.", "the Qwen slot scored it lower", "a GPT reviewer"):
        try:
            redaction.assert_clean(leak)
            check(False, f"seat talk is fatal: {leak!r}")
        except redaction.RedactionLeak:
            check(True, f"seat talk is fatal: {leak!r}")

    # ── accept waits for a panel report ─────────────────────────────────────
    src = (ROOT / "intake" / "apply_decision.py").read_text()
    i = src.find("panel_md, rqc_md = worker._scrubbed_report_pair(")
    j = src.find('if verdict == "accept" and not panel_md.strip() and not force:')
    k = src.find("publications_url_str: str | None = None")
    check(0 < i < j < k, "the no-report guard sits before the first side effect of an accept")

    # ── the stats snapshot never shrinks ────────────────────────────────────
    import stats
    rdir = tmp / "reviews"; rdir.mkdir()
    check(stats.compute_stats(str(rdir)).get("total_reviewed") == 0, "an empty reviews dir counts zero")
    cd = (ROOT / "crossref_deposit.py").read_text()
    g = cd.find("if (fresh.get(\"total_reviewed\") or 0) < published:")
    w = cd.find("stats_path = _stats.write_stats(config.REVIEWS_DIR, out)")
    check(0 < g < w, "a fresh count below the published total is never written")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALL GREEN' if not failures else f'{len(failures)} FAILURE(S)'}")
sys.exit(1 if failures else 0)
