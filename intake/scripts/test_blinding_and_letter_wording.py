#!/usr/bin/env python3
"""Surname blinding over multi-author entries and the verified creators; the
curation team's note in scope-reject letters; reference-list and verification
wording when a manuscript has no reference list; the public RQC status line on
any decision. Every fixture is synthetic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


import review_compaction as rc  # noqa: E402
import redaction as R  # noqa: E402
from intake import notify_author as na  # noqa: E402

# ── 1. surname blinding ────────────────────────────────────────────────────
print("1. surname blinding")
check(rc.author_surnames({"author_names": ["A. Example, B. Sample, C. Tester"]}) == ["Example", "Sample", "Tester"],
      "a comma-joined byline yields every surname")
check(rc.author_surnames({"author_names": ["Example, Ada"]}) == ["Example"], "'Surname, Given' stays one name")
check(rc.author_surnames({"author_names": ["Example, A."]}) == ["Example"], "'Surname, I.' stays one name")
check(rc.author_surnames({"author_names": ["Ada Example and Ben Sample"]}) == ["Example", "Sample"], "' and ' separates")
check(rc.author_surnames({"author_names": ["Ada Example; Ben Sample & Cy Tester"]}) == ["Example", "Sample", "Tester"],
      "';' and '&' separate")
check(rc.author_surnames({"author_names": ["Example, A., Sample, B."]}) == ["Example", "Sample"],
      "'Surname, I., Surname, I.' pairs up")
check(rc.author_surnames({"author_names": ["Ada Example"]}) == ["Example"], "a single name is unchanged")
m = {"author_names": ["A. Example, B. Sample, C. Tester"]}
out = rc.blind_aux_text("the Sample questions (C. Tester), see Example 2024", m)
check(all(s not in out for s in ("Example", "Sample", "Tester")), f"no surname left in the text: {out!r}")
m = {"author_names": ["A. Example"], "known_author_names": ["A. Example", "Ben Sample"]}
out = rc.blind_aux_text("as Sample and Example (2024) argue", m)
check("Sample" not in out and "Example" not in out, f"a creator the extraction missed is blinded too: {out!r}")


def _fake_claude(spans):
    return lambda text, *a, **k: (json.dumps(spans), "", 0)


paper = ("A Paper\nA. Example, B. Sample\nExample University\n\nIntro. As shown in Sample (2023) "
         "and Example (2024), the result holds.\n\n" + "Body text. " * 60)
orig_call = rc._claude_call
rc._claude_call = _fake_claude({"author_names": ["A. Example, B. Sample"], "affiliations": ["Example University"]})
try:
    redacted, manifest = rc.compact_paper(paper, log=lambda *_: None,
                                          known_identity={"names": ["A. Example", "B. Sample"], "other": []})
finally:
    rc._claude_call = orig_call
check(not manifest.get("_failure"), f"the synthetic compaction succeeds: {manifest.get('_failure')}")
check(manifest.get("known_author_names") == ["A. Example", "B. Sample"], "the manifest carries the verified creators")
blinded = rc.blind_aux_text(redacted, manifest)
check("Sample (2023)" not in blinded and "Example (2024)" not in blinded,
      "self-citations of every creator are blinded in the manuscript")

# ── 2. scope-reject letters carry the curation team's note ────────────────
print("2. scope-reject note slot")
sent: list[dict] = []
_orig_send = na.email_send.send_email
na.email_send.send_email = lambda **kw: (sent.append(kw), (True, "stub"))[1]
FULL = {"author_names": ["Ada Example"], "affiliations": ["Example University"],
        "emails": ["ada@example.com"], "orcids": ["0000-0000-0000-0000"],
        "references_count": 12, "references_section_chars": 900,
        "original_chars": 10000, "redacted_chars": 8000, "reduction_pct": 20.0}
NO_LIST = {"author_names": ["Ada Example"], "original_chars": 10000, "redacted_chars": 9900, "reduction_pct": 1.0}
common = dict(to="ada@example.com", sub_id="ICSAC-SUB-TEST-1000000000", title="A Test Paper",
              author_name="Ada Example", panel_report_md="")
NOTE = "The paper sits outside the scope of the institute."
try:
    for source in ("upload", "doi"):
        na.send_decision(verdict="scope_reject", source=source, compaction_manifest=FULL, curator_note=NOTE, **common)
        body = sent[-1]["body_md"] if sent else ""
        check(body.count(NOTE) == 1, f"scope_reject ({source}) carries the note exactly once")
        check(body.find(NOTE) < body.find("## Next steps"), f"scope_reject ({source}) puts it before Next steps")
        na.send_decision(verdict="scope_reject", source=source, compaction_manifest=FULL, **common)
        body = sent[-1]["body_md"] if sent else ""
        check("[CURATOR NOTE" in body, f"scope_reject ({source}) with no note shows the loud bracket")

    # ── 3. reference-list wording ─────────────────────────────────────────
    print("3. reference-list and verification wording")
    lead = na._compaction_lead(NO_LIST, citations_checked=False)
    check("references list was not removed" not in lead and "We found no reference list" in lead,
          "no list and nothing checked: the lead says so and claims no list was seen")
    check(na._compaction_lead(FULL, citations_checked=True) == na._compaction_lead(FULL),
          "a normal paper's lead is unchanged")
    na.send_decision(verdict="revise", source="upload", compaction_manifest=NO_LIST, citations_checked=False,
                     curator_findings="1. A finding.", **common)
    body = sent[-1]["body_md"] if sent else ""
    check("against your full reference list" not in body and "references list was not removed" not in body,
          "no list: neither false sentence reaches the letter")
    na.send_decision(verdict="revise", source="upload", compaction_manifest=FULL, citations_checked=False,
                     curator_findings="1. A finding.", **common)
    body = sent[-1]["body_md"] if sent else ""
    check("against your full reference list upstream" not in body
          and "Citation verification returned no results for your reference list." in body,
          "a list whose check returned nothing: the letter says so")
    for flag in (True, None):
        na.send_decision(verdict="revise", source="upload", compaction_manifest=FULL, citations_checked=flag,
                         curator_findings="1. A finding.", **common)
        body = sent[-1]["body_md"] if sent else ""
        check("Citation verification was performed separately against your full reference list upstream of the panel."
              in body, f"a checked paper (citations_checked={flag}) keeps the verification sentence")
finally:
    na.email_send.send_email = _orig_send

check(rc.panel_notice() == rc.PANEL_NOTICE_TEMPLATE and rc.panel_notice(True) == rc.PANEL_NOTICE_TEMPLATE,
      "the panel notice for a checked paper is byte-identical")
pn = rc.panel_notice(citations_checked=False)
check("performed separately upstream" not in pn and "No citation verification results exist" in pn,
      "the panel notice for an unchecked paper says nothing was verified")

# ── 4. public RQC status line ─────────────────────────────────────────────
print("4. public RQC status line")


def rqc(flag: bool, spec: str, inj: str):
    slot = lambda: {"reviewer": "x", "errored": False, "dimensions": [
        ("Rubric Adherence", "4/5", "Rubric followed."),
        ("Internal Consistency", "4/5", "Scores match the text."),
        ("Specificity", spec, "Generic in places."),
        ("Tone", "5/5", "Neutral."),
        ("Evidence Use", "4/5", "Uses the evidence block."),
        (R.RQC_INJECTION_DIM_LABEL, inj, "No signal."),
    ]}
    parsed = R.ParsedRQC(record_id="ICSAC-SUB-TEST-31B", title="A Test Paper", doi="",
                         audit_date="2026-01-01T00:00:00Z", flag=flag, summary="",
                         overall_concerns=[], slots=[slot() for _ in range(5)])
    md = R.build_public_rqc_markdown(parsed)
    R.assert_rqc_clean(md)
    return md


for spec, inj, what in (("3/5", "5/5", "shared pattern"), ("4/5", "4/5", "injection only")):
    md = rqc(True, spec, inj)
    check("before acceptance" not in md and "acceptance decision" not in md
          and "reviewed by the curation team before the decision." in md,
          f"a flagged record ({what}) names the decision, not an acceptance")
check("Review Quality Control: passed." in rqc(False, "4/5", "5/5"), "an unflagged record is unchanged")

print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(0 if not failures else 1)
