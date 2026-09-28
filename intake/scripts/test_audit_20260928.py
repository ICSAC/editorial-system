#!/usr/bin/env python3
"""Regression checks for the 2026-09-28 audit fixes. Offline: no network, no
mail, no pings; every write lands in one temporary directory that is removed
at the end, pass or fail. Run from the repo root with the venv interpreter:
    .venv/bin/python intake/scripts/test_audit_20260928.py
"""
from __future__ import annotations

import datetime as dt
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


tmp = Path(tempfile.mkdtemp(prefix="audit-20260928-"))
try:
    # ── 1. the DOI counter (items 1, 20) ─────────────────────────────────────
    import crossref_deposit as cd
    seq_file = tmp / "subs" / ".doi-seq"
    check(cd._next_seq(seq_file, 2026) == 1 and cd._next_seq(seq_file, 2026) == 2, "counter: 1 then 2")
    check(json.loads(seq_file.read_text()) == {"2026": 2}, "counter: file holds the state")
    check(not list(seq_file.parent.glob(".doi-seq.tmp-*")), "counter: no temp file left behind")
    seq_file.unlink()
    d = tmp / "subs" / "ICSAC-SUB-00042" / "crossref"; d.mkdir(parents=True)
    (d / "doi.json").write_text(json.dumps({"doi": "10.67697/icsac.2026.005"}))
    check(cd._next_seq(seq_file, 2026) == 6, "counter: a lost file resumes above the highest doi.json suffix")
    check(cd._next_seq(seq_file, 2027) == 1, "counter: another year starts at 1")
    seq_file.write_text("{not json")
    try:
        cd._next_seq(seq_file, 2026); check(False, "counter: corrupt file refused")
    except cd.CrossrefError:
        check(True, "counter: corrupt file refused")

    # ── 2. stage() keeps checkpoints (item 7) ────────────────────────────────
    src = Path.home() / "icsac-submissions" / "ICSAC-SUB-00008" / "submission.json"
    if src.exists():
        sub = tmp / "subs" / "ICSAC-SUB-00099"; (sub / "crossref").mkdir(parents=True)
        shutil.copyfile(src, sub / "submission.json")
        (sub / "crossref" / "staged.json").write_text(json.dumps(
            {"doi": "10.67697/icsac.2026.099", "checkpoints": {"crossref_batch_id": "b1", "crossref_deposited_at": "x"}}))
        cd.stage(sub, doi="10.67697/icsac.2026.099", log=lambda m: None)
        st = json.loads((sub / "crossref" / "staged.json").read_text())
        check(st.get("checkpoints", {}).get("crossref_batch_id") == "b1", "stage: an in-flight batch id survives a re-stage")
        check(st.get("doi") == "10.67697/icsac.2026.099" and (sub / "crossref" / "deposit.xml").exists(), "stage: XML + record written")
    else:
        print("  skip stage() check (no SUB-00008 submission.json on this host)")

    # ── 3. the publications registry keeps its fields (item 10) ─────────────
    import publications as pub
    reg = tmp / "accepted.json"
    reg.write_text(json.dumps([{
        "slug": "old-paper", "record_id": "1", "sub_id": "ICSAC-SUB-00001", "title": "Old paper",
        "authors": ["A. Author"], "doi": "10.5281/zenodo.1", "accepted_date": "2026-04-01",
        "source": "zenodo-community", "ssrn_id": "123", "license_url": "https://creativecommons.org/licenses/by/4.0/",
    }]))
    pub.REGISTRY_PATH = str(reg)
    out = pub.upsert_entry({"title": "Old paper", "authors": ["A. Author"], "doi": "10.5281/zenodo.1",
                            "source": "zenodo-community", "promotion_opt_out": True})
    check(out.get("sub_id") == "ICSAC-SUB-00001" and out.get("ssrn_id") == "123"
          and out.get("license_url", "").endswith("by/4.0/"), "registry: an update keeps sub_id, ssrn_id, licence")
    check(out.get("promotion_opt_out") is True, "registry: an update takes the new exclusion")
    new = pub.upsert_entry({"title": "New paper", "authors": ["B. Author"], "doi": "10.67697/icsac.2026.007",
                            "source": "submission-pdf", "sub_id": "ICSAC-SUB-00007",
                            "license_url": "https://creativecommons.org/licenses/by/4.0/",
                            "promotion_exclusions": ["social"], "persistence_opt_out": True})
    check(new.get("sub_id") == "ICSAC-SUB-00007" and new.get("license_url") and new.get("promotion_exclusions") == ["social"]
          and new.get("persistence_opt_out") is True, "registry: a new Crossref-path entry carries sub_id, licence, exclusions")
    check(list(new.keys())[:4] == ["slug", "sub_id", "title", "authors"], "registry: canonical key order")

    # ── 4. compaction fails closed and blinds the aux texts (items 2, 3) ────
    import review_compaction as rc
    paper = ("A Title\nJane M.\nDoe\nUniversity of Example\njdoe@example.edu\n\n"
             "1 Introduction\nJanedoe is a common word. Lithium is an element.\nReferences\n[1] X.")
    def fake(spans):
        rc._claude_call = lambda text, **kw: (json.dumps(spans), "", 0)
    fake({})
    txt, man = rc.compact_paper(paper, log=lambda m: None)
    check(txt == "" and man.get("_failure") == "no identity spans identified", "compaction: an empty extraction fails closed")
    fake({"author_names": ["Jane M. Doe"], "affiliations": ["University of Example"], "emails": ["jdoe@example.edu"]})
    txt, man = rc.compact_paper(paper, log=lambda m: None)
    check(not man.get("_failure") and "Jane" not in txt and "jdoe@" not in txt and "University of Example" not in txt,
          "compaction: a line-wrapped name is removed (whitespace-tolerant)")
    check("Janedoe" in txt and "Lithium" in txt, "compaction: bounded matching leaves longer words alone")
    check(man.get("author_names") == ["Jane M. Doe"], "compaction: the manifest records what was removed")
    fake({"author_names": ["Jane M. Doe"], "emails": ["nobody@example.org"]})
    txt, man = rc.compact_paper(paper, log=lambda m: None)
    check(txt == "" and man.get("_failure") == "identity span unmatched", "compaction: an unmatched email fails closed")
    aux = rc.blind_aux_text("By Jane M. Doe (jdoe@example.edu), ORCID 0000-0002-1825-0097, at University of Example.",
                            {"author_names": ["Jane M. Doe"], "affiliations": ["University of Example"], "emails": [], "orcids": []})
    check("Jane" not in aux and "@" not in aux and "0000-0002" not in aux and "University of Example" not in aux,
          "compaction: the abstract is blinded with the same spans plus email/ORCID patterns")

    # ── 5. review scoring, schema, negation, dedupe, chain safety (12–15) ───
    import review
    check(review._as_score(True) is None and review._as_score(5.9) is None and review._as_score(5.0) == 5
          and review._as_score("4") == 4 and review._as_score(3) == 3, "schema: only whole numbers are scores")
    check(review._has_unnegated_occurrence("The prose is not only padded but also fabricated.", "padded"),
          "negation: 'not only' does not negate")
    check(not review._has_unnegated_occurrence("There is no padding in this paper.", "padding"),
          "negation: a plain negation still counts")
    dims = {"domain_fit": {"mean": 4.0, "mean_raw": 4.0}}
    for k in ("methodological_transparency", "internal_consistency", "citation_integrity", "novelty_signal", "ai_provenance_signal"):
        dims[k] = {"mean": 3.4, "mean_raw": 27 / 8}
    rec = review._apply_thresholds(dims, ["REVIEW_FURTHER"] * 8)
    check(rec != "RECOMMEND", "thresholds: a 3.479 panel is not rounded over the 3.5 bar")
    dims_r = {k: {"mean": v["mean"]} for k, v in dims.items()}
    dims_r["domain_fit"]["mean"] = 4.0
    check(review._apply_thresholds({**dims_r, "novelty_signal": {"mean": 3.9}}, ["REVIEW_FURTHER"] * 8) in ("RECOMMEND", "REVIEW_FURTHER"),
          "thresholds: legacy dicts without mean_raw still evaluate")
    agg = review.compute_aggregate([{"model": "m1", "overall_recommendation": "RECOMMEND",
                                      **{k: {"score": 4, "justification": "x"} for k in review.config.RUBRIC_DIMENSIONS}}])
    check(all("mean_raw" in v for v in agg["dimension_scores"].values()), "aggregate: mean_raw carried")
    check(review._model_identity("hf|Qwen/Qwen3-235B-A22B-Instruct-2507:deepinfra")
          == review._model_identity("openrouter:qwen/qwen3-235b-a22b-instruct-2507:free")
          == review._model_identity("oai|sambanova|Qwen3-235B-A22B-Instruct-2507"), "dedupe: one model, three labels, one identity")
    check(review._model_identity("or|google/gemma-4-31b-it:free") != review._model_identity("or|google/gemma-4-26b-a4b-it:free"),
          "dedupe: different models stay distinct")
    orig_slot, orig_retries = review._run_slot, getattr(review.config, "MAX_SLOT_RETRIES", 1)
    review.config.MAX_SLOT_RETRIES = 0
    review._run_slot = lambda prompt, i, s, record_id=None, pass_idx=0: {
        "model": "hf|Qwen/X:deepinfra" if i in (1, 2) else f"m{i}", "overall_recommendation": "RECOMMEND"}
    res = review._run_single_pass("p", [None, "a", "b", "c"], 3)
    check("error" in res[2] and "duplicate" in res[2]["error"] and "error" not in res[1], "dedupe: the second seat of the same model is not counted")
    review._run_slot, review.config.MAX_SLOT_RETRIES = orig_slot, orig_retries
    orig_hf = review.run_hf_router_review
    def boom(*a, **k): raise AttributeError("'NoneType' object has no attribute 'get'")
    review.run_hf_router_review = boom
    r = review._run_panel_chain("p", ["hf|x:y"])
    check(isinstance(r, dict) and "error" in r and "AttributeError" in r["error"], "chain: a malformed provider response costs one entry, not the pass")
    review.run_hf_router_review = orig_hf

    # ── 6. the author window: gate, issue, ping-once (items 5, 6, 21) ───────
    from intake import author_approval as aa
    sub = tmp / "subs" / "ICSAC-SUB-00050"; sub.mkdir(parents=True)
    (sub / "submission.json").write_text(json.dumps({"title": "T", "creators": [{"name": "A B"}], "form": {"name": "A B", "email": "a@example.com"}}))
    (sub / "state.json").write_text(json.dumps({"decision": "accept", "completed_at": "2026-09-28T10:00:00Z"}))
    ok, why = aa.gate(sub)
    check(ok is False and "no author window" in why, "gate: a post-09-27 acceptance without a window is refused")
    (sub / "state.json").write_text(json.dumps({"decision": "accept", "completed_at": "2026-05-01T10:00:00Z"}))
    ok, why = aa.gate(sub)
    check(ok is True, "gate: a pre-09-27 acceptance without a window passes")
    ap = aa.issue(sub)
    check(ap["url"].startswith("https://") and (sub / "author_approval.json").exists(), "issue: a window opens")
    rec = json.loads((sub / "author_approval.json").read_text())
    rec["status"] = "approved"; rec["responses"] = [{"choice": "approve", "at": "2026-09-28T11:00:00Z"}]
    (sub / "author_approval.json").write_text(json.dumps(rec))
    try:
        aa.issue(sub); check(False, "issue: refuses to overwrite a recorded response")
    except RuntimeError:
        check(True, "issue: refuses to overwrite a recorded response")
    ap2 = aa.issue(sub, force=True)
    check(bool(ap2["url"]), "issue: force re-issues")
    # check_windows: ping failure keeps the flag down
    w = tmp / "subs" / "ICSAC-SUB-00051"; w.mkdir()
    (w / "state.json").write_text(json.dumps({"decision": "accept", "completed_at": "2026-09-01T00:00:00Z"}))
    (w / "author_approval.json").write_text(json.dumps({"token_sha256": "x", "issued_at": "2026-09-01T00:00:00Z",
        "deadline": "2026-09-08T00:00:00Z", "status": "pending", "responses": [], "window_closed_pinged": False}))
    aa.SUBMISSIONS_ROOT = tmp / "subs"
    aa._ping = lambda m: False
    n = aa.check_windows()
    check(n == 0 and json.loads((w / "author_approval.json").read_text())["window_closed_pinged"] is False,
          "check_windows: a failed ping is retried next tick")
    aa._ping = lambda m: True
    n = aa.check_windows()
    check(n == 1 and json.loads((w / "author_approval.json").read_text())["window_closed_pinged"] is True,
          "check_windows: a delivered ping is recorded once")

    # ── 7. citation recheck never downgrades (item 17) ───────────────────────
    import citation_verify as cv
    orig_vc = cv.verify_citation
    def raises(c): raise RuntimeError("registries down")
    cv.verify_citation = raises
    out = cv.verify_all([{"raw": "Doe 2020", "verified": True, "resolver": "crossref", "resolved_id": "10.1/x"},
                         {"raw": "Roe 2021", "verified": False}])
    check(out[0].get("verified") is True and out[0].get("resolved_id") == "10.1/x" and "recheck_reason" in out[0],
          "citations: a failed recheck keeps the earlier verification")
    check(out[1].get("verified") is False and "verifier raised" in str(out[1].get("reason")), "citations: an unverified one records the failure")
    cv.verify_citation = orig_vc

    # ── 8. panel_retry: a dead DOI resolution is stuck, not parked (item 19) ─
    import panel_retry as pr
    now = dt.datetime.now(dt.timezone.utc)
    old = (now - dt.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check(pr.classify({"state": "resolving_doi", "resolving_at": old}, now)[0] == "stuck", "retry: resolving_doi > 3 h is stuck")
    check(pr.classify({"state": "resolving_doi", "resolving_at": fresh}, now)[0] == "running", "retry: a fresh resolution is running")
    check(pr.wait_started({"state": "resolving_doi", "resolving_at": old}, "stuck") is not None, "retry: the stuck clock starts at the resolution")

    # ── 9. decide.sh refuses a second decision (item 6) ─────────────────────
    from intake import apply_decision as ad
    t2 = tmp / "subs" / "test" / "ICSAC-SUB-TEST-1700000001"; t2.mkdir(parents=True)
    (t2 / "submission.json").write_text(json.dumps({"title": "T", "tier": 2, "test_mode": True, "source": "upload",
                                                    "form": {"name": "A", "email": "a@example.com"}}))
    (t2 / "state.json").write_text(json.dumps({"state": "completed", "decision": "accept", "completed_at": "2026-09-28T00:00:00Z"}))
    ad.TEST_SUBMISSIONS_ROOT = tmp / "subs" / "test"
    os.environ.pop("ICSAC_DECISION_FORCE", None)
    rc_code = ad.main(["apply_decision", "ICSAC-SUB-TEST-1700000001", "accept", "note"])
    check(rc_code == 3, "decide: a second accept on a decided paper does nothing (exit 3)")

    # ── 10. pass B: SVG gate, download cap ──────────────────────────────────
    from intake import sponsor_logo as sl
    check(sl._svg_is_inert(b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1" fill="#000"/></svg>'), "svg: a plain logo passes")
    check(not sl._svg_is_inert(b'<svg><script>alert(1)</script></svg>'), "svg: script refused")
    check(not sl._svg_is_inert(b'<svg><rect onload="x()"/></svg>'), "svg: event handler refused")
    check(not sl._svg_is_inert(b'<svg><image href="https://evil.example/x.png"/></svg>'), "svg: external reference refused")
    check(not sl._svg_is_inert(b'<!DOCTYPE svg [<!ENTITY x "y">]><svg/>'), "svg: entity declaration refused")
    check(sl._svg_is_inert(b'<svg><image href="data:image/png;base64,AAAA"/><use href="#a"/></svg>'), "svg: data and fragment references pass")
    import submission_intake as si
    class FakeResp:
        def __init__(self): self.left = 3 * 1024 * 1024
        def read(self, n):
            if self.left <= 0: return b""
            n = min(n, self.left); self.left -= n; return b"x" * n
        def __enter__(self): return self
        def __exit__(self, *a): return False
    orig_open = si.urllib.request.urlopen
    si.urllib.request.urlopen = lambda req, timeout=120: FakeResp()
    os.environ["INTAKE_MAX_PDF_BYTES"] = str(1024 * 1024)
    dest = tmp / "dl"; dest.mkdir()
    r = si.download_pdf({"id": "12345", "files": [{"key": "paper.pdf", "links": {"self": "https://zenodo.example/x"}}]}, str(dest))
    check(r is None and not list(dest.iterdir()), "download: an oversized hosted PDF is refused and removed")
    os.environ.pop("INTAKE_MAX_PDF_BYTES", None)
    si.urllib.request.urlopen = orig_open
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(0 if not failures else 1)
