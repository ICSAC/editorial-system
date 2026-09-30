#!/usr/bin/env python3
"""Citation identity. Offline: every
registry call is stubbed; nothing is written outside one temporary directory
that is removed at the end, pass or fail. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_citation_identity.py

Regression cases, each of which an earlier version of the
verifier reported to the panel as REAL:
  Varela 2024  -- the cited DOI resolves to a different paper (mismatch)
  Case B  -- the cited DOI 404s; the real record has another DOI (dead)
  Case C  -- the cited DOI 404s (dead)
  Case D  -- no DOI; author-year search found a different preprint
plus: a Zenodo dataset DOI confirmed through DataCite; a short registry title
("Less Is Enough") confirmed; HTML in a Crossref title; a DOI with no cited
title judged on authors + year; verify_all keeping the cited title and letting
a DOI disagreement downgrade an earlier fuzzy "verified"; the report's
sections and wording; the header line; the CiteStamp coverage-gap log; and the
code package digest on a synthetic archive (no network).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


TMP = Path(tempfile.mkdtemp(prefix="icsac-citid-"))
try:
    import config
    config.REVIEWS_DIR = str(TMP / "reviews")
    import citation_verify as cv
    import citestamp_check as cc
    cc.COVERAGE_GAP_LOG = str(TMP / "reviews" / "citestamp_coverage_gaps.jsonl")

    # ── registry stubs ────────────────────────────────────────────────
    CROSSREF = {
        "10.0000/jfs.2024.0011": {"resolver": "crossref", "resolved_id": "10.0000/jfs.2024.0011",
                                       "title": "A survey of tidal pool ecology",
                                       "abstract": "", "year": 2024, "authors": ["Hana Okafor", "Lev Brandt"]},
        "10.0000/jfs.2024.0042": {"resolver": "crossref", "resolved_id": "10.0000/jfs.2024.0042",
                                       "title": "Measuring lichen growth on coastal rocks: A field study",
                                       "abstract": "", "year": 2024, "authors": ["Ines Varga", "Omar Haddad", "Tomas Lind"]},
        "10.0000/jpc.2023.0929": {"resolver": "crossref", "resolved_id": "10.0000/jpc.2023.0929",
                                       "title": "Folded lattices: a layered study of crease patterns in paper models",
                                       "abstract": "", "year": 2025, "authors": ["Iryna Quarrie", "Katrina Teller"]},
        "10.0000/fsci.1972.393": {"resolver": "crossref", "resolved_id": "10.0000/fsci.1972.393",
                                         "title": "Less Is Enough", "abstract": "", "year": 1972, "authors": ["P. W. Hollis"]},
        "10.0000/lfp.1987.381": {"resolver": "crossref", "resolved_id": "10.0000/lfp.1987.381",
                                       "title": "Self-similar ripples: An explanation of the 1/\n <i>k</i>\n spectrum",
                                       "abstract": "", "year": 1987, "authors": ["Per Holm", "Chao Lin", "Kurt Weber"]},
        "10.0000/jcm.1961.183": {"resolver": "crossref", "resolved_id": "10.0000/jcm.1961.183",
                               "title": "Heat and Order in Clockwork Mechanisms",
                               "abstract": "", "year": 1961, "authors": ["R. Lorimer"]},
    }
    DATACITE = {
        "10.5281/zenodo.0000000": {"resolver": "datacite", "resolved_id": "10.5281/zenodo.0000000",
                                    "title": "Tide Gauge Readings: A Sample Data Set",
                                    "abstract": "", "year": 2025, "authors": ["Author, Ada", "Author, Ben", "Author, Cy"]},
    }
    BIBLIO = {
        "Quarrie": CROSSREF["10.0000/jpc.2023.0929"],
        "Brook": {"resolver": "crossref", "resolved_id": "10.0000/preprint.1000002",
                    "title": "Rooftop Gardens and Pollinator Visits: A Structural Survey",
                    "abstract": "", "year": 2026, "authors": ["Alan Brook"]},
    }
    calls = {"crossref": [], "datacite": [], "arxiv": 0, "s2": 0, "biblio": 0}

    def _cr(doi):
        calls["crossref"].append(doi)
        return dict(CROSSREF.get(doi.lower())) if doi.lower() in CROSSREF else None

    def _dc(doi):
        calls["datacite"].append(doi)
        return dict(DATACITE.get(doi.lower())) if doi.lower() in DATACITE else None

    def _arxiv(terms, year=None):
        calls["arxiv"] += 1
        return None

    def _s2(query, year=None):
        calls["s2"] += 1
        return None

    def _biblio(raw, year=None):
        calls["biblio"] += 1
        for key, rec in BIBLIO.items():
            if key in raw:
                return dict(rec)
        return None

    cv._fetch_crossref, cv._fetch_datacite = _cr, _dc
    cv._search_arxiv, cv._search_semanticscholar, cv._search_crossref_bibliographic = _arxiv, _s2, _biblio

    # ── the four regression cases ─────────────────────────────────────
    print("[1] Varela 2024: the cited DOI belongs to another paper")
    vac = {"raw": "Varela, G., Serra, V. D. P., Lorca, V., et al. (2024). Measuring lichen growth on coastal rocks: A field study. Journal of Field Studies, 8(12), 101–119.",
           "authors": ["G. Varela", "V. D. P. Serra", "V. Lorca"], "year": 2024,
           "title": "Measuring lichen growth on coastal rocks: A field study",
           "doi": "10.0000/jfs.2024.0011", "arxiv_id": None, "type": "doi", "claim_context": ""}
    v = cv.verify_citation(dict(vac))
    check(v["verified"] is False, "not verified")
    check(v["doi_identity"] == "mismatch", f"doi_identity == mismatch ({v['doi_identity']})")
    check(v["confidence"] == "doi-mismatch", "confidence doi-mismatch")
    check("tidal pool ecology" in v["reason"] and "not to the cited" in v["reason"], "reason names the record the DOI points at")
    check(v.get("resolved_title", "").startswith("A survey of"), "resolved_title kept for the report")
    check("authors differ" in v["reason"], "reason says the authors differ too")

    print("[2] Quarrie 2025: dead DOI, the real record is found by title and named, not verified")
    mal = {"raw": "Quarrie, I., & Teller, K. (2025). Folded lattices: A layered study of crease patterns in paper models. Journal of Paper Craft, 45(4), 925–950.",
           "authors": ["I. Quarrie", "K. Teller"], "year": 2025,
           "title": "Folded lattices: A layered study of crease patterns in paper models",
           "doi": "10.0000/jpc.2024.0422", "arxiv_id": None, "type": "doi", "claim_context": ""}
    v = cv.verify_citation(dict(mal))
    check(v["verified"] is False, "not verified")
    check(v["doi_identity"] == "dead", "doi_identity == dead")
    check(v["confidence"] == "doi-dead", "confidence doi-dead")
    check(v.get("suggested_id") == "10.0000/jpc.2023.0929", f"suggested_id names the real record ({v.get('suggested_id')})")
    check("does not resolve on Crossref or DataCite" in v["reason"] and "registry search finds" in v["reason"], "reason: dead + suggestion")
    check("10.0000/jpc.2024.0422" in calls["datacite"], "DataCite was tried after Crossref 404")

    print("[3] Moreau 2024: dead DOI, nothing found by title")
    yag = {"raw": "Moreau, K. G. (2024). Towards a pocket barometer. Instrument Notes, 3, 1933.",
           "authors": ["K. G. Moreau"], "year": 2024, "title": "Towards a pocket barometer",
           "doi": "10.0000/inst.2024.126", "arxiv_id": None, "type": "doi", "claim_context": ""}
    v = cv.verify_citation(dict(yag))
    check(v["verified"] is False and v["doi_identity"] == "dead", "dead, not verified")
    check("suggested_id" not in v, "no suggestion invented")

    print("[4] Brook 2026: no DOI; an author-year hit with a different title is not REAL")
    bra = {"raw": "Brook, A. (2026). Seasonal patterns in urban pigeon roosting [Preprint]. SSRN. https://ssrn.com/abstract=1000001",
           "authors": ["A. Brook"], "year": 2026, "title": "Seasonal patterns in urban pigeon roosting",
           "doi": None, "arxiv_id": None, "type": "title-only", "claim_context": ""}
    v = cv.verify_citation(dict(bra))
    check(v["verified"] is False, "not verified")
    check(v.get("doi_identity") is None, "no DOI, no doi_identity")
    check("different work" in v["reason"] and "Rooftop" in v["reason"], f"reason names the other paper ({v['reason'][:90]})")

    # ── confirmations that must keep working ──────────────────────────
    print("[5] confirmations")
    zen = {"raw": "Author, A., Author, B., & Author, C. (2025b). Tide Gauge Readings: A Sample Data Set (Version 1.0) [Data set]. Zenodo.",
           "authors": ["A. Author", "B. Author", "C. Author"], "year": 2025,
           "title": "Tide Gauge Readings: A Sample Data Set (Version 1.0) [Data set]",
           "doi": "10.5281/zenodo.0000000", "arxiv_id": None, "type": "doi", "claim_context": ""}
    v = cv.verify_citation(dict(zen))
    check(v["verified"] and v["doi_identity"] == "confirmed" and v["resolver"] == "datacite", "Zenodo dataset confirmed via DataCite")
    and_ = {"raw": "Hollis, P. W. (1972). Less is enough: Small samples and the nature of the layered structure of field notes. Field Science, 177(4047), 393–396.",
            "authors": ["P. W. Hollis"], "year": 1972,
            "title": "Less is enough: Small samples and the nature of the layered structure of field notes",
            "doi": "10.0000/fsci.1972.393", "arxiv_id": None, "type": "doi", "claim_context": ""}
    v = cv.verify_citation(dict(and_))
    check(v["verified"] and v["doi_identity"] == "confirmed" and v["confidence"] == "exact-id", "short registry title (Less Is Enough) confirmed")
    bak = {"raw": "Holm, P., Lin, C., & Weber, K. (1987). Self-similar ripples: An explanation of 1/k spectrum. Letters in Field Physics, 59(4), 381–384.",
           "authors": ["P. Holm", "C. Lin", "K. Weber"], "year": 1987,
           "title": "Self-similar ripples: An explanation of 1/k spectrum",
           "doi": "10.0000/lfp.1987.381", "arxiv_id": None, "type": "doi", "claim_context": ""}
    v = cv.verify_citation(dict(bak))
    check(v["verified"] and v["doi_identity"] == "confirmed", "HTML tags in the Crossref title do not break the match")
    notitle = {"raw": "Lorimer, R. (1961). J. Clock. Mech. 5, 183.", "authors": ["R. Lorimer"], "year": 1961,
               "title": None, "doi": "10.0000/jcm.1961.183", "arxiv_id": None, "type": "doi", "claim_context": ""}
    v = cv.verify_citation(dict(notitle))
    check(v["verified"] and v["doi_identity"] == "confirmed" and "no cited title" in v["reason"], "no cited title: authors + year confirm the DOI")
    wrongyear = dict(notitle, year=1999, authors=["Q. Nobody"])
    v = cv.verify_citation(dict(wrongyear))
    check(v["verified"] is False and v["doi_identity"] == "mismatch", "no cited title, wrong authors and year: mismatch")

    # ── verify_all: cited title survives; a DOI finding downgrades ────
    print("[6] verify_all")
    res = cv.verify_all([dict(vac), dict(and_), dict(vac, verified=True, confidence="author-year-match")])
    check(res[0]["cited_title"] == vac["title"], "cited_title kept on a mismatch")
    check(res[1]["cited_title"] == and_["title"] and res[1]["title"] == "Less Is Enough", "cited_title kept; title = registry title")
    check(res[2]["verified"] is False and res[2]["doi_identity"] == "mismatch", "an earlier fuzzy 'verified' is downgraded by its own DOI")
    counts = cv.identity_counts(res)
    check(counts == {"confirmed": 1, "mismatch": 2, "dead": 0}, f"identity_counts {counts}")

    # ── the report ───────────────────────────────────────────────────
    print("[7] report")
    rep_in = cv.verify_all([dict(vac), dict(mal), dict(yag), dict(bra), dict(zen), dict(and_)])
    rep = cv.build_verification_report(rep_in)
    check("ground truth" not in rep, "the report no longer calls itself ground truth")
    check("### Confirmed by identifier" in rep and "### Cited DOI disagrees" in rep, "sections present")
    check("Varela et al. 2024** — DOI MISMATCH" in rep, "Varela listed as DOI MISMATCH")
    check(re.search(r"Quarrie[^\n]*— DOI DEAD\.", rep) and "10.0000/jpc.2023.0929" in rep, "Quarrie listed as DOI DEAD with the real record")
    check("Brook 2026** — UNVERIFIABLE" in rep and "different work" in rep, "Brook unverifiable with the reason")
    check("— REAL." not in rep, "no line says REAL any more")
    check("A. Author et al. 2025b** — CONFIRMED" in rep and "Hollis 1972** — CONFIRMED" in rep, "confirmed lines")
    check("MATCHED" in rep and "existence check" in rep, "the fuzzy evidence level is explained")
    cv.save_citation_report("ICSAC-SUB-TEST-1", rep_in, rep)
    import review
    line = review._citation_header_line("ICSAC-SUB-TEST-1")
    check(line is not None and "DOI identity: 1 mismatch, 2 dead" in line, f"header line carries identity counts ({line})")
    prompt_src = review.REVIEW_PROMPT_TEMPLATE
    check("(c) IDENTIFIER DISAGREEMENT" in prompt_src and "HOLD IN THE PAPER'S OWN EXAMPLES" in prompt_src
          and "RELABELLING" in prompt_src and "CODE PACKAGE DIGEST" in prompt_src, "panel prompt: identity + assumptions + relabelling + code digest")

    # ── CiteStamp coverage-gap log ────────────────────────────────────
    print("[8] coverage gaps")
    results = [{"doi": "10.5281/zenodo.0000000", "in_graph": False, "edges": 0, "error": None},
               {"doi": "10.0000/fsci.1972.393", "in_graph": True, "edges": 3653, "error": None},
               {"doi": "10.0000/jfs.2024.0011", "in_graph": False, "edges": 0, "error": None}]
    n = cc._log_coverage_gaps(rep_in, results, log=lambda m: None)
    check(n == 1, f"one gap logged (confirmed + absent), not the mismatch ({n})")
    rows = [json.loads(l) for l in open(cc.COVERAGE_GAP_LOG)]
    check(rows[0]["doi"] == "10.5281/zenodo.0000000" and rows[0]["title"].startswith("Tide Gauge"), "gap row carries DOI + registry title")

    # ── code digest on a synthetic archive ───────────────────────────
    print("[9] code digest")
    import code_digest as cdg
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/README.md", "# Examples\nRun case_alpha.py\n")
        zf.writestr("pkg/case_alpha.py",
                    "import math\n\ndef compute_xrm(G, k):\n    b = k\n    XRM = a * b + c\n    return XRM\n\ndef main():\n    pass\n")
        zf.writestr("pkg/case_beta.py", "def xrm_fraction(G):\n    XRM = hits / trials\n    return XRM\n")
        zf.writestr("pkg/out.csv", "t,v\n0,12\n")
        zf.writestr("pkg/big.bin", b"\0" * 10)
    rows = cdg.expand([{"name": "zenodo.zip", "size": buf.tell(), "data": buf.getvalue()}])
    check(len(rows) == 5 and rows[-1]["text"] is None, "members listed; binary not read")
    text = "The XRM index and its XRM-2 variant ... XRM rises with load ... the NPT survey ... XRM XRM NPT NPT NPT DOI DOI DOI"
    terms = cdg.constructs_from_text(text)
    check("XRM" in terms and "NPT" in terms and "DOI" not in terms, f"constructs from text {terms}")
    md = cdg.render("Zenodo record 1 (test)", "Record title: t", rows, terms)
    check("case_alpha.py: compute_xrm, main" in md, "definitions inventory")
    check("case_alpha.py:5: XRM = a * b + c" in md
          and "case_beta.py:2: XRM = hits / trials" in md, "the two XRM definitions surface with file:line")
    check("NPT: not found in any source file" in md, "a construct absent from the code is said so")
    check("README head" in md and "Run case_alpha.py" in md, "README head shown")
    check(cdg.classify("https://doi.org/10.5281/zenodo.0000000") == ("zenodo", "22152184")
          and cdg.classify("https://github.com/o/r.git") == ("github", "o/r")
          and cdg.classify("https://osf.io/abc")[0] == "other", "link classification")
    links = cdg.links_from({"code_data": {"url": "https://doi.org/10.5281/zenodo.0000000"},
                            "related_identifiers": [{"identifier": "10.5281/zenodo.0000000", "relation": "isSupplementTo"},
                                                    {"identifier": "10.20944/x", "relation": "isNewVersionOf"}]},
                           "see https://github.com/o/r for code and 10.5281/zenodo.0000000 again")
    check(links[0].endswith("22152184") and "10.5281/zenodo.0000000" in links and any("github.com/o/r" in l for l in links)
          and not any("10.20944" in l for l in links) and len(links) == 3, f"links: declared, supplements, in-text; deduped ({links})")
    config.CODE_DIGEST_ENABLED = False
    md, meta = cdg.build({"code_data": {"url": "https://doi.org/10.5281/zenodo.1"}}, "x")
    check("disabled" in md and meta["enabled"] is False, "ICSAC_CODE_DIGEST=0 path")
    config.CODE_DIGEST_ENABLED = True
    md, meta = cdg.build({"code_data": {"url": "https://osf.io/abc"}}, "x")
    check(md.startswith("(declared but not fetched") and meta["skipped"], "unknown host: named, not fetched, no exception")
    md, meta = cdg.build({}, "")
    check(md.startswith("(none"), "no links: (none)")

    # ── the revise letter (B3) ────────────────────────────────────────
    print("[10] revise letter")
    from intake import notify_author as na
    for tpl in ("submission_revise_upload.md", "submission_revise_doi.md"):
        raw = (na.TEMPLATES_DIR / tpl).read_text()
        check("panel found the work substantive" not in raw, f"{tpl}: the false sentence is gone")
        check("{{curator_findings}}" in raw and "Curation team findings" in raw, f"{tpl}: findings section present")
    subj, body = na._render("submission_revise_upload.md", {
        "icsac_submission_id": "ICSAC-SUB-TEST-1", "title": "T", "author_name": "A",
        "attachments_block": "(att)", "compaction_disclosure": "(disc)",
        "curator_findings": "1. Fix the unit in Table 2.\n2. Define the sample window."})
    check("1. Fix the unit in Table 2." in body and "the decision is the curation team's" in body, "findings rendered")
    subj, body = na._render("submission_revise_upload.md", {
        "icsac_submission_id": "ICSAC-SUB-TEST-1", "title": "T", "author_name": "A",
        "attachments_block": "(att)", "compaction_disclosure": "(disc)",
        "curator_findings": "[CURATION TEAM FINDINGS - x]"})
    check("[CURATION TEAM FINDINGS" in body, "empty findings leave the loud bracket")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
