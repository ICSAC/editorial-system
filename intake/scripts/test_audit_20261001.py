#!/usr/bin/env python3
"""Regression checks for the audit of 2026-10-01: the decision letter's
blind-review disclosure and the public Review Quality Control render.
Offline: no network, no mail (the mail call is stubbed), nothing written.
Run from the repo root:
    .venv/bin/python intake/scripts/test_audit_20261001.py

Covers: the disclosure says only what the manifest shows was removed and
names what stayed visible (a letter claimed the references list was removed
when its start marker had not matched); a failed preprocessing step never
names the tool that failed; a public RQC shows only the dimensions the audit
scored, so an audit made before the Evidence Use dimension existed does not
claim it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


from intake import notify_author as na  # noqa: E402
import redaction  # noqa: E402

FULL = {"author_names": ["Ada Example"], "affiliations": ["Example University"],
        "emails": ["ada@example.com"], "orcids": ["0000-0000-0000-0000"],
        "references_count": 12, "references_section_chars": 900,
        "original_chars": 10000, "redacted_chars": 8000, "reduction_pct": 20.0}
NO_REFS = dict(FULL, references_count=0, references_section_chars=0,
               match_failures=[{"category": "references_section",
                                "snippet": "start_marker: References"}])
NOTHING = {"original_chars": 10000, "redacted_chars": 10000, "reduction_pct": 0.0}

print("disclosure lead")
lead = na._compaction_lead(FULL)
check("removed author names, affiliations, contact information, ORCID iDs and the "
      "references list (inline citation markers were preserved)" in lead,
      "a full removal lists every removed item")
check("not removed" not in lead and "stayed visible" not in lead,
      "a full removal claims nothing stayed visible")
lead = na._compaction_lead(NO_REFS)
check("removed author names, affiliations, contact information and ORCID iDs." in lead,
      "an unmatched references section: the identity items are still listed")
check("references list (inline" not in lead and "The references list was not removed" in lead,
      "an unmatched references section is never claimed removed, and the letter says so")
lead = na._compaction_lead(NOTHING)
check("found nothing to remove" in lead and "Author details in the text stayed visible" in lead
      and "The references list was not removed" in lead,
      "an empty manifest says nothing was removed and what the panel saw")

print("whole letter (mail call stubbed)")
sent: list[dict] = []
_orig = na.email_send.send_email


def _stub(**kw):
    sent.append(kw)
    return True, "stub"


na.email_send.send_email = _stub
try:
    common = dict(to="ada@example.com", sub_id="ICSAC-SUB-TEST-1000000000",
                  title="A Test Paper", author_name="Ada Example", verdict="revise",
                  source="upload", panel_report_md="", curator_findings="1. A finding.")
    na.send_decision(compaction_manifest=NO_REFS, **common)
    body = sent[-1]["body_md"] if sent else ""
    check(sent and sent[-1].get("draft") is True, "the decision is a draft, never a send")
    check("references list (inline citation markers were preserved)" not in body,
          "the letter does not claim the unmatched references list was removed")
    check("double-blind" not in body, "the letter does not call a partial removal double-blind")
    check("The references list was not removed" in body, "the letter says the references stayed")
    sent.clear()
    na.send_decision(compaction_manifest={"_failure": "claude timeout"}, **common)
    body = (sent[-1]["body_md"] if sent else "").lower()
    check(bool(body) and "claude" not in body and "timeout" not in body,
          "a preprocessing failure reason never reaches the letter")
    check("did not complete" in body, "the failure is still disclosed")
finally:
    na.email_send.send_email = _orig

print("public RQC render")


def _parsed(dims: list[str]) -> "redaction.ParsedRQC":
    slots = [{"reviewer": f"Reviewer {i}", "errored": False,
              "dimensions": [(d, "4", "Cites section 3 and the methods table.") for d in dims]}
             for i in (1, 2)]
    return redaction.ParsedRQC(record_id="X", title="A Test Paper", doi="",
                               audit_date="2026-10-01", flag=False, summary="", slots=slots)


V1 = ["Rubric Adherence", "Internal Consistency", "Specificity", "Tone", "Injection Indicators"]
V2 = V1[:4] + ["Evidence Use", "Injection Indicators"]
pub = redaction.build_public_rqc_markdown(_parsed(V1))
check("Evidence Use" not in pub, "an audit without Evidence Use shows no Evidence Use column")
check("verification evidence" not in pub, "and does not claim the evidence check")
check("four dimensions above" in pub, "and counts four dimensions")
check("injection" not in pub.lower(), "the internal dimension stays out")
pub = redaction.build_public_rqc_markdown(_parsed(V2))
check("| Evidence Use |" in pub, "an audit with Evidence Use shows the column")
check("verification evidence" in pub and "five dimensions above" in pub,
      "and names the evidence check and five dimensions")
check("injection" not in pub.lower(), "the internal dimension stays out (v2)")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    sys.exit(1)
print("all ok")
