#!/usr/bin/env python3
"""The review-quality auditor sees what the panel saw about references.

The panel's citation block carries a misattribution section (load-bearing
references judged against the cited work); the auditor's evidence block did
not, so a slot that named a misattribution read to the auditor as unanchored.
The auditor's block was also one 6,000-char cut, so a long reference list was
cut mid-list and the code digest after it never arrived. And the auditor
called a valid panel verdict (REVIEW_FURTHER) out of vocabulary. Offline.

    .venv/bin/python intake/scripts/test_rqc_evidence_misattribution.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402
import review_quality_control as rqc  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def citation(i: int, **kw) -> dict:
    base = {"authors": [f"R. Author{i:03d}"], "year": 1990 + i % 30, "raw": f"Author{i:03d}, R. ({1990 + i % 30}). A work."}
    base.update(kw)
    return base


def payload(n_unver: int, n_unconf: int, n_conf: int) -> dict:
    cits, i = [], 0
    cits.append(citation(i, verified=False, confidence="doi-dead", doi_identity="dead",
                         reason="Cited DOI 10.0000/dead.1 does not exist at doi.org.")); i += 1
    for _ in range(n_conf):
        cits.append(citation(i, verified=True, confidence="exact-id", doi_identity="confirmed")); i += 1
    for _ in range(n_unver):
        cits.append(citation(i, verified=False, confidence="unverifiable",
                             reason="No registry record matched the cited title and authors; a book or report "
                                    "outside the registries searched.")); i += 1
    for _ in range(n_unconf):
        cits.append(citation(i, verified=True, confidence="title-year-match")); i += 1
    mis = [{"citation_id": 1, "label": "Author001 1991", "supports": "yes", "reason": "The record's abstract states the claim."},
           {"citation_id": 2, "label": "Author002 1992", "supports": "no",
            "reason": "The cited work models a different mechanism from the one the claim attributes to it."},
           {"citation_id": 3, "label": "Author003 1993", "supports": "unsure", "reason": "No abstract on record."}]
    return {"record_id": "x", "citations": cits, "misattribution": mis}


DIGEST = ("# Code package digest\n\n```\nSource: Zenodo record 1 (declared by the author)\nRecord title: t\n\n"
          "Definitions per source file:\n  pkg/model.py: simulate, fit\n\n"
          "Paper constructs searched in the code: XRM\n  XRM:\n    pkg/model.py:12: XRM = a * b\n\n"
          "Files (2, grouped by folder and type):\n  pkg/model.py  (100 bytes)\n```\n")

with tempfile.TemporaryDirectory(prefix="icsac-rqcev-") as td:
    rdir = Path(td)
    print("[1] the misattribution verdicts are in the auditor's block, before the UNCONFIRMED list")
    rid = "ICSAC-SUB-TEST-1790300001"
    (rdir / f"{rid}_citations.json").write_text(json.dumps(payload(3, 3, 6)))
    (rdir / f"{rid}_code_digest.md").write_text(DIGEST)
    ev = rqc.build_evidence_block(rid, str(rdir))
    check("MISATTRIBUTION CHECK" in ev, "a misattribution section")
    check("Author002 1992 — NO: The cited work models a different mechanism" in ev, "the NO verdict with its reason")
    check("Author003 1993 — UNSURE" in ev and "Author001 1991 — YES" in ev, "UNSURE and YES verdicts too")
    check("MISATTRIBUTION CHECK" in ev and "UNCONFIRMED: no identifier" in ev
          and ev.index("MISATTRIBUTION CHECK") < ev.index("UNCONFIRMED: no identifier"),
          "the verdicts come before the UNCONFIRMED list")

    print("[2] no verdicts on record: said so, never silence")
    p = payload(1, 1, 2)
    del p["misattribution"]
    (rdir / "ICSAC-SUB-TEST-1790300002_citations.json").write_text(json.dumps(p))
    ev2 = rqc.build_evidence_block("ICSAC-SUB-TEST-1790300002", str(rdir))
    check("no verdicts recorded" in ev2 and "unchecked" in ev2, "a missing check reads as unchecked")

    print("[3] a 100-reference paper: nothing cut mid-list, the digest still arrives")
    rid3 = "ICSAC-SUB-TEST-1790300003"
    (rdir / f"{rid3}_citations.json").write_text(json.dumps(payload(25, 35, 39)))
    (rdir / f"{rid3}_code_digest.md").write_text(DIGEST)
    ev3 = rqc.build_evidence_block(rid3, str(rdir))
    check("[evidence block truncated]" not in ev3 and "not shown (size cap)" not in ev3, "the citation list is whole")
    check(ev3.count("— UNVERIFIABLE") == 25 and ev3.count("— UNCONFIRMED") == 35, "every UNVERIFIABLE and UNCONFIRMED line")
    check("pkg/model.py:12: XRM = a * b" in ev3, "the code digest's construct line arrives after a long list")
    check(len(ev3) <= rqc.EVIDENCE_CAP + 40, f"within the cap ({len(ev3)})")

print("[4] the auditor knows the panel's four verdicts")
prompt = rqc.build_prompt("---\nrecord_id: x\n---\n# Review\n", "evidence")
check(all(v in prompt for v in review.VALID_RECOMMENDATIONS), "every valid panel verdict is named in the prompt")
check("REVIEW_FURTHER (the slot routes the paper to" in prompt and "Never score a slot" in prompt,
      "REVIEW_FURTHER is called valid, not drift")
rubric = (ROOT / "rubrics" / "review_quality_control.md").read_text()
check(all(f"`{v}`" in rubric for v in review.VALID_RECOMMENDATIONS), "the rubric names all four too")

with tempfile.TemporaryDirectory(prefix="icsac-rqcev2-") as td:
    rdir = Path(td)
    print("[5] many DOI findings with long reasons do not push the verdicts out")
    p = payload(2, 2, 4)
    long_reason = "Cited DOI 10.0000/dead.x does not exist at doi.org. " + "A registry search names no record. " * 8
    p["citations"] += [citation(500 + i, verified=False, confidence="doi-dead", doi_identity="dead",
                                authors=["A Very Long Corporate Author Name For A Standards Body " * 3],
                                reason=long_reason) for i in range(60)]
    (rdir / "ICSAC-SUB-TEST-1790300005_citations.json").write_text(json.dumps(p))
    ev5 = rqc.build_evidence_block("ICSAC-SUB-TEST-1790300005", str(rdir))
    check("Author002 1992 — NO:" in ev5, "the NO verdict is still in the block")
    check("more DOI findings not shown" in ev5, "the DOI findings say how many were left out")
    check(len(ev5) <= rqc.EVIDENCE_CAP, f"within the cap ({len(ev5)})")

    print("[6] a blinding error withholds the block instead of passing it unblinded")
    import review_compaction  # noqa: E402
    real_blind = review_compaction.blind_aux_text

    def _boom(text, manifest):
        raise ValueError("bad span")
    review_compaction.blind_aux_text = _boom
    try:
        ev6 = rqc.build_evidence_block("ICSAC-SUB-TEST-1790300005", str(rdir),
                                       {"author_names": ["Ada Author"], "affiliations": [], "emails": [], "orcids": []})
    finally:
        review_compaction.blind_aux_text = real_blind
    check("withheld" in ev6 and "MISATTRIBUTION" not in ev6, f"withheld on a blinding error ({ev6[:60]!r})")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
