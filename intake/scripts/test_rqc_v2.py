#!/usr/bin/env python3
"""Review Quality Control v2 (2026-10-01): the evidence_use dimension and the
evidence block. Offline: no claude call; one temporary directory, removed at
the end, pass or fail. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_rqc_v2.py

Covers: the evidence block built from a citations JSON + a code digest (counts,
the lines the panel had to act on, the digest's definitions, blinding, caps,
the no-evidence note); the prompt carrying it and six dimensions with the
re-anchored scale; normalisation and the flag on evidence_use; the internal
rendering; the public redaction carrying Evidence Use and never the injection
dimension; the rubric, letters and site copy naming the new dimension.
"""
from __future__ import annotations

import json
import os
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


tmp = Path(tempfile.mkdtemp(prefix="rqc-v2-"))
try:
    import config
    import review_quality_control as rqc
    import redaction

    rid = "ICSAC-SUB-TEST-1790200001"
    rdir = tmp / "reviews"; rdir.mkdir()
    cits = {"record_id": rid, "citations": [
        {"authors": ["P. W. Hollis"], "year": 1972, "verified": True, "confidence": "exact-id", "doi_identity": "confirmed"},
        {"authors": ["G. Varela", "V. Serra"], "year": 2024, "verified": False, "confidence": "doi-mismatch",
         "doi_identity": "mismatch", "reason": "DOI 10.1038/x resolves to *A survey of tidal pool ecology*, not to the cited title."},
        {"authors": ["K. G. Moreau"], "year": 2024, "verified": False, "confidence": "doi-dead", "doi_identity": "dead",
         "reason": "Cited DOI 10.0000/inst.2024.126 does not resolve on Crossref or DataCite."},
        {"authors": ["H. Hale"], "year": 1983, "verified": True, "confidence": "title-author-match"},
        {"authors": ["A. Brook"], "year": 2026, "verified": False, "confidence": "unverifiable", "reason": "closest record is a different work"},
    ]}
    (rdir / f"{rid}_citations.json").write_text(json.dumps(cits))
    (rdir / f"{rid}_code_digest.md").write_text(
        "# Code package digest\n\nFetched read-only.\n\n```\nSource: Zenodo record 1 (declared by the author)\nRecord title: t\n\n"
        "Files (2):\n  a.py  (10 bytes)\n\nREADME head (README.md, first 40 lines):\n  by [author] [author]\n\n"
        "Definitions per source file:\n  a.py: compute_xrm\n\nPaper constructs searched in the code: XRM\n  XRM:\n    a.py:5: xrm = a * b + c  # by [author] [author]\n```\n\nMeta: {}\n")

    print("1. evidence block")
    ev = rqc.build_evidence_block(rid, str(rdir))
    check("5 references; 1 CONFIRMED by identifier, 1 MATCHED by text only, 2 with a DOI that disagrees (mismatch or dead), 1 UNVERIFIABLE" in ev, "counts line")
    check("Varela et al. 2024 — DOI MISMATCH" in ev and "Moreau 2024 — DOI DEAD" in ev, "the lines the panel had to act on")
    check("Brook 2026 — UNVERIFIABLE" in ev and "Hale 1983 — MATCHED by text [title-author-match]" in ev, "unverifiable + matched lines")
    check("CODE PACKAGE DIGEST" in ev and "a.py: compute_xrm" in ev and "a.py:5: xrm = a * b + c" in ev, "digest definitions + construct lines")
    check("Files (2)" not in ev and "README head" not in ev, "file list and README are left out (the auditor needs definitions, not the inventory)")
    ev_b = rqc.build_evidence_block(rid, str(rdir), {"author_names": ["[author] [author]"], "affiliations": [], "emails": [], "orcids": []})
    check("[author] [author]" not in ev_b and "[withheld]" in ev_b, "blinded with the manifest's spans")
    check(rqc.build_evidence_block("ICSAC-SUB-TEST-0000000000", str(rdir)) == rqc.NO_EVIDENCE_NOTE, "nothing on disk -> the no-evidence note")
    big = dict(cits); big["citations"] = cits["citations"] * 400
    (rdir / "BIG_citations.json").write_text(json.dumps(big))
    check(len(rqc.build_evidence_block("BIG", str(rdir))) <= rqc.EVIDENCE_CAP + 40, "capped")

    print("2. prompt")
    prompt = rqc.build_prompt("---\nrecord_id: x\n---\n# Review\n", ev)
    check("<<<EVIDENCE_SUPPLIED>>>" in prompt and "Varela et al. 2024 — DOI MISMATCH" in prompt, "prompt carries the evidence block")
    check("5. evidence_use" in prompt and "6. injection_indicators" in prompt and '"evidence_use":' in prompt, "six dimensions + JSON schema")
    check("earns a 4, not a 5" in prompt, "re-anchored scale in the prompt")
    check(prompt.rindex("<<<EVIDENCE_SUPPLIED>>>") < prompt.rindex("<<<PANEL_REVIEW>>>"), "evidence precedes the untrusted panel block")
    check(rqc.NO_EVIDENCE_NOTE in rqc.build_prompt("x", ""), "empty evidence -> the note, never a blank block")

    print("3. normalise, flag, render")
    raw = {"review_quality_control_flag": False, "summary": "s", "slots": [
        {"reviewer": "Reviewer 1", "errored": False,
         "rubric_adherence": {"score": 4, "justification": "a"}, "internal_consistency": {"score": 4, "justification": "b"},
         "specificity": {"score": 4, "justification": "c"}, "tone": {"score": 4, "justification": "d"},
         "evidence_use": {"score": 1, "justification": "said citations verified against two DOI DEAD lines"},
         "injection_indicators": {"score": 5, "justification": "e"}}], "overall_concerns": ["x"]}
    norm = rqc._normalize(raw)
    check(norm["slots"][0]["evidence_use"]["score"] == 1, "evidence_use normalised")
    check(rqc._recompute_flag(norm) is True, "evidence_use <= 2 trips the flag")
    md = rqc._render_markdown({"title": "T", "record_id": rid, "doi": ""}, norm)
    check("- **Evidence Use** (1/5): said citations verified" in md and "five scholarly dimensions" in md, "internal rendering shows Evidence Use")
    path = rqc.save_rqc({"title": "T", "record_id": rid, "doi": ""}, norm, out_dir=str(tmp / "out"))
    check(path.startswith(str(tmp / "out")) and os.path.exists(path), "out_dir redirects the file (calibration runs never touch the live record)")

    print("4. public redaction")
    parsed = redaction.parse_rqc_file(path)
    pub = redaction.build_public_rqc_markdown(parsed)
    check("Evidence Use" in pub and "1/5" in pub or "Evidence Use" in pub, "public RQC carries Evidence Use")
    check("Injection" not in pub and "injection" not in pub, "public RQC carries no injection dimension")
    check("use of the verification evidence" in pub, "public sentence names the new dimension")
    check("Evidence Use" in redaction.RQC_PUBLIC_DIMENSIONS and "Injection Indicators" not in redaction.RQC_PUBLIC_DIMENSIONS, "public dimension list")

    print("5. copy")
    rub = (ROOT / "rubrics" / "review_quality_control.md").read_text()
    check("### 5. evidence_use (public)" in rub and "### 6. injection_indicators" in rub and "5 is exemplary" in rub, "rubric: new dimension + re-anchored scale")
    for t in ("submission_accept_upload_pending.md", "submission_received.md"):
        txt = (ROOT / "intake" / "templates" / t).read_text()
        check("use of the verification evidence" in txt, f"{t} names the new dimension")
    src = (ROOT / "review_quality_control.py").read_text()
    check("compaction_manifest" in (ROOT / "review.py").read_text().split("rqc_mod.audit_review(")[1][:120], "review.py passes the compaction manifest to the audit")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAIL: {len(failures)} failure(s)")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
