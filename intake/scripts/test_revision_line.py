#!/usr/bin/env python3
"""A decision letter for a revised paper names the earlier submission it
revises, in the header under the submission ID; a first submission's letter
carries no such line. Every decision template carries the slot, and both
callers of send_decision pass the recorded link. Offline, mail call stubbed,
no files written. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_revision_line.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from intake import notify_author as na  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


DECISION_TEMPLATES = ("accept_doi", "accept_upload", "accept_upload_pending", "revise_codelink",
                      "revise_doi", "revise_upload", "scope_reject_doi", "scope_reject_upload")
for t in DECISION_TEMPLATES:
    raw = (na.TEMPLATES_DIR / f"submission_{t}.md").read_text()
    check("**Submission ID:** {{icsac_submission_id}}{{revision_line}}\n" in raw,
          f"submission_{t}.md: the revision slot sits on the submission ID line")
for t in ("received", "intake_failure"):
    raw = (na.TEMPLATES_DIR / f"submission_{t}.md").read_text()
    check("{{revision_line}}" not in raw, f"submission_{t}.md: no slot (its sender fills no such key)")

sent: list[dict] = []
_orig = na.email_send.send_email


def _stub(**kw):
    sent.append(kw)
    return True, "stub"


na.email_send.send_email = _stub
try:
    common = dict(to="ada@example.com", sub_id="ICSAC-SUB-TEST-2000000002",
                  title="A Test Paper", author_name="Ada Example",
                  panel_report_md="", curator_findings="1. A finding.",
                  compaction_manifest={"author_names": ["Ada Example"]})
    for verdict, source in (("revise", "upload"), ("revise", "doi"), ("scope_reject", "upload")):
        sent.clear()
        na.send_decision(verdict=verdict, source=source, revision_of="ICSAC-SUB-TEST-2000000001",
                         **common)
        body = sent[-1]["body_md"] if sent else ""
        check("**Submission ID:** ICSAC-SUB-TEST-2000000002\n**Revision of:** ICSAC-SUB-TEST-2000000001\n**Title:**"
              in body, f"{verdict}/{source}: a revision's letter names the earlier submission under its ID")
        sent.clear()
        na.send_decision(verdict=verdict, source=source, **common)
        body = sent[-1]["body_md"] if sent else ""
        check(bool(body) and "Revision of" not in body
              and "**Submission ID:** ICSAC-SUB-TEST-2000000002\n**Title:**" in body,
              f"{verdict}/{source}: a first submission's letter carries no revision line")
finally:
    na.email_send.send_email = _orig

for caller, expr in (("intake/apply_decision.py", 'revision_of=state_pre.get("revision_of") or ""'),
                     ("intake/submission_worker.py", 'revision_of=state.get("revision_of") or ""')):
    src = (ROOT / caller).read_text()
    check(src.count("send_decision(") == 1 and expr in src.split("send_decision(", 1)[1].split("\n    )")[0],
          f"{caller}: the send_decision call passes the recorded link")

sys.exit(1 if failures else 0)
