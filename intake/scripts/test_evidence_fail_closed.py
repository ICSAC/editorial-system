#!/usr/bin/env python3
"""Evidence fails closed. Offline: every registry, model and CiteStamp call is
stubbed; one temporary directory, removed at the end, pass or fail. Run from
the repo root with the venv:
    .venv/bin/python intake/scripts/test_evidence_fail_closed.py

Only a reference whose identity is confirmed by its own identifier can ground
an integrity finding. A record found by text search for a reference with no
identifier is UNCONFIRMED: the panel and the review-quality auditor see the
level and never the record; the misattribution check never judges it;
CiteStamp is never asked about it (nor about a DOI that points to another
work); a deposit never links it as a <doi>. And blind-review compaction lets a
manuscript through with no author spans only when none of the submission's own
names, ORCID iDs, emails or affiliations occurs in its text.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


TMP = Path(tempfile.mkdtemp(prefix="icsac-failclosed-"))
try:
    import config
    config.REVIEWS_DIR = str(TMP / "reviews")
    import citation_verify as cv
    import citation_misattribution as cm
    import citestamp_check as cc
    cc.COVERAGE_GAP_LOG = str(TMP / "reviews" / "gaps.jsonl")
    cc.CALL_GAP = 0

    confirmed = {"raw": "Holm, P. (1999). Ripples on sand. Field Notes, 3, 1-9.", "authors": ["P. Holm"], "year": 1999,
                 "title": "Ripples on sand", "cited_title": "Ripples on sand", "doi": "10.0000/fn.1999.1",
                 "verified": True, "confidence": "exact-id", "doi_identity": "confirmed",
                 "resolver": "crossref", "resolved_id": "10.0000/fn.1999.1", "abstract": "ripples", "claim_context": "ripples form"}
    book = {"raw": "Moss, R. (2001). A Field Guide to Lichens. Example Press.", "authors": ["R. Moss"], "year": 2001,
            "title": "Review of: A Field Guide to Lichens", "cited_title": "A Field Guide to Lichens", "doi": None,
            "verified": True, "confidence": "title-year-match", "resolver": "crossref",
            "resolved_id": "10.0000/rev.2002.77", "abstract": "a review", "claim_context": "lichens grow slowly"}
    with_doi_fuzzy = dict(book, raw="Moss, R. (2001b). Lichen dates. Example Press.", doi="10.0000/lich.2001",
                          confidence="title-author-match", resolved_id="10.0000/other.9")
    dead = {"raw": "Nobody, A. (2020). Gone. J. Lost, 1.", "authors": ["A. Nobody"], "year": 2020, "title": "Gone",
            "doi": "10.0000/gone.1", "verified": False, "confidence": "doi-dead", "doi_identity": "dead",
            "reason": "Cited DOI 10.0000/gone.1 does not exist at doi.org."}
    mismatch = {"raw": "Nobody, B. (2021). Wrong. J. Else, 2.", "authors": ["B. Nobody"], "year": 2021, "title": "Wrong",
                "doi": "10.0000/else.2", "verified": False, "confidence": "doi-mismatch", "doi_identity": "mismatch",
                "reason": "DOI 10.0000/else.2 resolves to another work."}
    web = {"raw": "Example Society. 2025. \"Rules.\" https://example.org/rules/.", "authors": ["Example Society"],
           "year": 2025, "title": "Rules", "verified": False, "confidence": "web-source", "resolved_id": "https://example.org/rules/"}
    cits = [confirmed, book, with_doi_fuzzy, dead, mismatch, web]

    print("[1] levels")
    check(cv.is_identity_confirmed(confirmed) and not cv.is_identity_confirmed(book)
          and not cv.is_identity_confirmed(with_doi_fuzzy) and not cv.is_identity_confirmed(dead), "only exact-id is confirmed")
    check(cv.is_unconfirmed_match(book) and cv.is_unconfirmed_match(with_doi_fuzzy)
          and not cv.is_unconfirmed_match(web) and not cv.is_unconfirmed_match(confirmed), "text matches are unconfirmed; web sources are not")

    print("[2] the panel's report")
    rep = cv.build_verification_report(cits)
    check("UNCONFIRMED" in rep and "not evidence of anything" in rep, "level + legend")
    check("10.0000/rev.2002.77" not in rep and "Review of:" not in rep and "10.0000/other.9" not in rep,
          "the text-search record is never shown")
    check("MATCHED" not in rep, "no MATCHED level left")
    check('Submission claim context: "lichens grow slowly"' in rep, "the claim context stays")
    check("Moss 2001** — UNCONFIRMED" in rep, "the unconfirmed line names the reference")

    print("[3] misattribution sees confirmed references only")
    sel = cm.select_load_bearing(cits, "text")
    check([c["raw"] for c in sel] == [confirmed["raw"]], f"eligible = the confirmed one ({len(sel)})")

    print("[4] CiteStamp is asked about confirmed DOIs only")
    asked = []
    cc.server_info = lambda: {"name": "stub"}
    def _tool(name, identifier, limit=25, rpc_id=2):
        asked.append(identifier)
        return {"groundings": [], "page": {"inferred_total": 0}}
    cc.tool = _tool
    res = cc.check_citations(cits, log=lambda m: None)
    check(set(asked) == {"10.0000/fn.1999.1"} and res["queried"] == 1, f"queried {sorted(set(asked))}")

    print("[5] a deposit links confirmed DOIs only")
    import crossref_deposit as cd
    root = ET.Element("x")
    cd._citations(root, cits)
    dois = [e.text for e in root.iter() if e.tag.endswith("doi")]
    check(dois == ["10.0000/fn.1999.1"], f"<doi> links {dois}")

    print("[6] the review-quality auditor's evidence block")
    cv.save_citation_report("ICSAC-SUB-TEST-FC", cits, rep)
    import review_quality_control as rqc
    ev = rqc.build_evidence_block("ICSAC-SUB-TEST-FC", config.REVIEWS_DIR)
    check("2 UNCONFIRMED (no identifier cited; not evidence)" in ev and "Moss 2001 — UNCONFIRMED" in ev, "counts + line")
    check("10.0000/rev.2002.77" not in ev and "title-year-match" not in ev, "no record, no match mode")

    print("[7] header + prompt")
    import review
    line = review._citation_header_line("ICSAC-SUB-TEST-FC")
    check(line is not None and line.startswith("1/5 resolved · 2 unconfirmed · 1 cited by URL"), f"header ({line})")
    check("UNCONFIRMED (no identifier cited" in review.REVIEW_PROMPT_TEMPLATE
          and "MATCHED (found by text only" not in review.REVIEW_PROMPT_TEMPLATE, "panel prompt legend")
    import json
    payload = json.load(open(Path(config.REVIEWS_DIR) / "ICSAC-SUB-TEST-FC_citations.json"))
    check(payload.get("confirmed_count") == 1 and payload.get("unconfirmed_count") == 2, "JSON counts")
    print("[8] compaction: a manuscript anonymized at submission goes on; anything else still fails closed")
    import review_compaction as rc
    paper = ("Ripples on sand: a field study\n\nAbstract. We measure ripples.\n\n1. Introduction\n"
             "Ripples form under flow (Holm 1999).\n\nReferences\nHolm, P. (1999). Ripples on sand.\n")
    rc._claude_call = lambda manuscript, **k: ('{"author_names": [], "affiliations": [], "emails": [], "orcids": []}', "", 0)
    who = {"names": ["Quill Ashdown"], "other": ["0000-0002-1825-0097", "q.ashdown@example.org", "Example Institute of Sand"]}
    txt, man = rc.compact_paper(paper, log=lambda m: None, known_identity=who)
    check(txt and not man.get("_failure") and man.get("anonymized_at_submission") is True,
          "no spans + none of the submission's identifiers in the text -> proceeds, flagged")
    check("anonymized at submission" in rc.render_manifest(man), "the author's disclosure says so")
    for label, text in (("surname", paper + "\nThanks to Ashdown.\n"), ("ORCID", paper + "0000-0002-1825-0097\n"),
                        ("email", paper + "q.ashdown@example.org\n"), ("affiliation", paper + "Example Institute of Sand\n")):
        txt, man = rc.compact_paper(text, log=lambda m: None, known_identity=who)
        check(txt == "" and man.get("_failure") == "no identity spans identified", f"no spans + the {label} in the text -> fails closed")
    txt, man = rc.compact_paper(paper, log=lambda m: None)
    check(txt == "" and man.get("_failure"), "no spans and no known identity -> fails closed (unchanged)")
    txt, man = rc.compact_paper(paper, log=lambda m: None, known_identity={"names": [], "other": ["x@example.org"]})
    check(txt == "" and man.get("_failure"), "no spans and no known NAME -> fails closed")
    import review
    ki = review._known_identity({"creators": [{"name": "Quill Ashdown", "orcid": "0000-0002-1825-0097", "affiliation": "X"}, "Doe, Jane"],
                                 "raw_metadata": {"submission": {"submitter_email": "q@example.org"}}})
    check(ki == {"names": ["Quill Ashdown", "Doe, Jane"], "other": ["0000-0002-1825-0097", "X", "q@example.org"]}, f"known identity {ki}")
    print("[9] no verification results is said as such, never as a clean result")
    rd = {"title": "T", "creators": ["A"], "full_text": "x" * 300}
    stub = "## Citation verification\n\nCitation verification was unavailable for this submission (X).\n"
    for label, rep in (("no report", ""), ("unavailable stub", stub)):
        pr = review.build_prompt(rd, verification_report=rep)
        check("Nothing about its references is established" in pr
              and pr.count("Do not write that any reference") == 1, f"{label}: the panel is told nothing was checked")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
