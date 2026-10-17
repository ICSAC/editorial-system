#!/usr/bin/env python3
"""Replay real submissions through intake, up to the panel and never into it,
and grade the evidence layer against a hand-checked answer key.

The corpus lives OUTSIDE the repository (unpublished author content). Its path
comes from --corpus or ICSAC_REPLAY_CORPUS; each paper is a directory with
    paper.pdf, submission.json   as received
    extraction.json              a frozen run of the reference extractor
    key.json                     the answer key (format below)

Everything runs in a shadow directory: config.REVIEWS_DIR and the CiteStamp
coverage-gap log point into it, notifications and pain signals are recorded
instead of sent, the misattribution check records which references it would
judge but calls no model, and the first panel call raises before any request
is made. Nothing is written to the live submission, its state or the public
status page. The shadow directory is removed at the end unless --keep.

Stages replayed (each from the code under --code, default this checkout):
code availability -> reference extraction (frozen by default; --extraction
live calls the extractor) -> citation verification -> misattribution
selection -> CiteStamp -> code package digest -> blind-review compaction ->
the panel prompt (captured, not sent).

Answer key (key.json):
  {"references": [{"find": <regex on the raw reference>, "level": L,
                   "id": <right record id>, "alt_ids": [...], "ok_ids": [...],
                   "wrong_ids": [...], "suggest": <id>, "note": ""}],
   "extraction": {"count": n},
   "code_availability": {"missing_link": bool},
   "code_digest": {"fetched": bool, "must_contain": [...]},
   "compaction": {"panel_must_not_contain": [...], "min_kept_ratio": 0.5},
   "evidence": {"must_contain": [...], "must_not_contain": [...]}}
  L is CONFIRMED, DOI MISMATCH, DOI DEAD, WEB SOURCE or UNCONFIRMED (no
  identifier was cited: an abstention or a match to one of ok_ids is right).

Check groups. A run is green when every HARD group passes:
  identity       HARD  levels and record ids of references with an identifier
                       or a URL; a wrong record reported as the cited one fails
  extraction     HARD  every key reference found (count checked in live mode)
  code_avail     HARD  the code/data link precheck
  code_digest    HARD  the digest fetched (or not) and names what it must
  compaction     HARD  no author identifier reaches the panel prompt
  evidence       HARD  the panel-facing citation block says what the key says
  fail_closed    --bar 2+  only identity-confirmed references can ground an
                       integrity finding: misattribution and CiteStamp see
                       confirmed references only, and a text-only match is
                       shown to the panel as unconfirmed, without the record
  fuzzy          --bar 3   no text-only match to a wrong record at all

Usage (repo root, prod venv):
  ICSAC_REPLAY_CORPUS=~/corpus .venv/bin/python intake/scripts/replay.py
  ... --paper P1 --code /path/to/older/checkout --skip compaction,citestamp
Exit 0 when every paper is green at the requested bar, 1 otherwise.
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
DEFAULT_CODE = HERE.parents[2]
STAGES = ("citestamp", "digest", "compaction")


class StopBeforePanel(Exception):
    """Raised by the first panel call; carries the prompt it would have sent."""

    def __init__(self, prompt: str):
        super().__init__("replay stops before the panel")
        self.prompt = prompt


# ── identifiers and levels (independent of the code under test) ──────────
def norm_id(v) -> str:
    s = str(v or "").strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if s.startswith(p):
            s = s[len(p):]
    m = re.fullmatch(r"(?:arxiv:|10\.48550/arxiv\.)(\d{4}\.\d{4,5})(v\d+)?", s)
    return f"arxiv:{m.group(1)}" if m else s.rstrip("/.")


def level_of(c: dict) -> str:
    """The evidence level a verified citation record carries, read from its raw
    fields so that the grader does not trust the labeller it is grading."""
    if c.get("confidence") == "web-source":
        return "WEB SOURCE"
    ident = c.get("doi_identity")
    if not c.get("verified") and ident == "mismatch":
        return "DOI MISMATCH"
    if not c.get("verified") and ident == "dead":
        return "DOI DEAD"
    if c.get("verified") and c.get("confidence") == "exact-id":
        return "CONFIRMED"
    if c.get("verified") or c.get("candidate_id"):
        return "MATCHED"
    return "UNVERIFIABLE"


def record_id_of(c: dict) -> str:
    return norm_id(c.get("resolved_id") or c.get("candidate_id"))


# ── the run ──────────────────────────────────────────────────────────────
def _load_code(code_dir: Path):
    for name in list(sys.modules):
        if name.split(".")[0] in ("config", "review", "citation_verify", "citation_misattribution",
                                  "citestamp_check", "code_digest", "code_availability", "review_compaction",
                                  "submission_intake", "notify", "intake", "redaction", "publications",
                                  "review_quality_control", "panel_retry"):
            del sys.modules[name]
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != DEFAULT_CODE]
    sys.path.insert(0, str(code_dir))
    return importlib.import_module("config")


def run_paper(paper_dir: Path, code_dir: Path, extraction: str, skip: set, shadow: Path, log) -> dict:
    config = _load_code(code_dir)
    config.REVIEWS_DIR = str(shadow / "reviews")
    os.makedirs(config.REVIEWS_DIR, exist_ok=True)
    out = {"events": [], "notifications": [], "stages": {}}
    rid = "ICSAC-SUB-TEST-REPLAY-" + re.sub(r"[^A-Za-z0-9]", "", paper_dir.name)

    def rec(name):
        def _f(*a, **k):
            out["notifications"].append(name)
            return None
        return _f

    import notify
    for name in dir(notify):
        if not name.startswith("_") and callable(getattr(notify, name)) and name[0].islower():
            setattr(notify, name, rec(f"notify.{name}"))
    import review
    for name in ("_fire_compaction_pain", "_fire_pain", "fire_pain"):
        if hasattr(review, name):
            setattr(review, name, rec(f"review.{name}"))

    def _guard(prompt=None, *a, **k):
        raise StopBeforePanel(prompt if isinstance(prompt, str) else "")
    for name in ("_run_single_pass", "_run_panel_chain", "run_claude_review", "run_openrouter_review"):
        if hasattr(review, name):
            setattr(review, name, _guard)

    import citation_verify
    frozen = json.loads((paper_dir / "extraction.json").read_text())
    if extraction == "frozen":
        citation_verify.extract_citations = lambda text, record_id, *a, **k: copy.deepcopy(frozen)

    try:
        import citestamp_check
        citestamp_check.COVERAGE_GAP_LOG = str(shadow / "reviews" / "citestamp_coverage_gaps.jsonl")
    except ImportError:
        citestamp_check = None
    if "citestamp" in skip and hasattr(review, "_run_citestamp_check"):
        review._run_citestamp_check = lambda record_id, citations, report, *a, **k: report

    import citation_misattribution as cm
    seen = {"eligible": None, "checked": None}
    real_select = cm.select_load_bearing

    def _select(citations, full_text, *a, **k):
        eligible = real_select(citations, full_text, max_n=10 ** 6)
        seen["eligible"] = [copy.deepcopy(c) for c in eligible]
        return eligible

    def _check(load_bearing, *a, **k):
        seen["checked"] = [copy.deepcopy(c) for c in load_bearing]
        return []   # no model is called in a replay
    cm.select_load_bearing, cm.check_misattribution_batch = _select, _check

    if "digest" in skip and hasattr(review, "_run_code_digest"):
        review._run_code_digest = lambda review_data, *a, **k: "(code package digest skipped by replay)"
    if "compaction" in skip:
        import review_compaction
        review_compaction.compact_paper = lambda text, *a, **k: (text, {
            "original_chars": len(text), "redacted_chars": len(text), "reduction_pct": 0, "replay_skipped": True})

    # review_data exactly as the worker builds it (a copy of the paper dir; the
    # worker's builder only reads submission.json and paper.pdf)
    sub_dir = shadow / "submission"
    sub_dir.mkdir()
    for f in ("paper.pdf", "submission.json"):
        shutil.copyfile(paper_dir / f, sub_dir / f)
    submission = json.loads((sub_dir / "submission.json").read_text())
    try:
        from intake import submission_worker
        review_data = submission_worker._build_review_data(rid, sub_dir)
        out["builder"] = "worker"
    except Exception as exc:
        import submission_intake
        out["builder"] = f"harness ({type(exc).__name__})"
        review_data = {"record_id": rid, "title": submission.get("title") or "", "creators": submission.get("creators") or [],
                       "description": submission.get("abstract") or "", "keywords": submission.get("keywords") or [],
                       "pdf_path": str(sub_dir / "paper.pdf"),
                       "full_text": submission_intake.extract_pdf_text(str(sub_dir / "paper.pdf")),
                       "submission": {"code_data": submission.get("code_data") or {},
                                      "related_identifiers": submission.get("related_identifiers") or []}}
    review_data["record_id"] = rid

    t0 = time.time()
    try:
        import code_availability
        out["code_availability"] = code_availability.assess(submission, review_data.get("full_text", ""))
    except ImportError:
        out["code_availability"] = None
    out["stages"]["code_availability"] = round(time.time() - t0, 1)

    prompt = None
    t0 = time.time()
    try:
        markdown, aggregate = review.review_paper(review_data)
        out["stopped"] = f"review_paper returned before the panel: {aggregate.get('recommendation')}"
        out["aggregate"] = {k: aggregate.get(k) for k in ("recommendation", "reason")}
        out["compaction_manifest"] = aggregate.get("compaction_manifest")
    except StopBeforePanel as stop:
        prompt = stop.prompt
        out["stopped"] = "before the panel"
    out["stages"]["review_paper_until_panel"] = round(time.time() - t0, 1)
    out["prompt"] = prompt or ""
    cj = shadow / "reviews" / f"{rid}_citations.json"
    out["citations"] = json.loads(cj.read_text()) if cj.exists() else {}
    cmd = shadow / "reviews" / f"{rid}_citations.md"
    out["report"] = cmd.read_text() if cmd.exists() else ""
    dg = shadow / "reviews" / f"{rid}_code_digest.md"
    out["digest"] = dg.read_text() if dg.exists() else ""
    out["misattribution_eligible"] = seen["eligible"]
    out["full_text_chars"] = len(review_data.get("full_text") or "")
    if out.get("compaction_manifest") is None and prompt is not None:
        out["compaction_manifest"] = "(in prompt)"
    return out


# ── grading ──────────────────────────────────────────────────────────────
def _find(cits: list, rx: str) -> list:
    r = re.compile(rx, re.I)
    return [c for c in cits if r.search(c.get("raw") or "")]


def grade(key: dict, run: dict, bar: int, skip: set, extraction: str) -> dict:
    groups = {g: [] for g in ("identity", "extraction", "code_avail", "code_digest", "compaction",
                              "evidence", "fail_closed", "fuzzy")}
    cits = (run.get("citations") or {}).get("citations") or []
    prompt = run.get("prompt") or ""
    if not prompt:
        groups["evidence"].append(f"no panel prompt captured ({run.get('stopped')})")

    def fail(group, msg):
        groups[group].append(msg)

    keyed = []
    for k in key.get("references", []):
        hits = _find(cits, k["find"])
        if len(hits) != 1:
            fail("extraction", f"{k['find']!r}: {len(hits)} extracted references match (want 1)")
            continue
        c = hits[0]
        keyed.append((k, c))
        got, rid = level_of(c), record_id_of(c)
        want = k["level"]
        right = {norm_id(x) for x in [k.get("id")] + k.get("alt_ids", []) + k.get("ok_ids", []) if x}
        wrong = {norm_id(x) for x in k.get("wrong_ids", [])}
        tag = f"{k['find']!r}: want {want}, got {got}" + (f" -> {rid}" if rid else "")
        if want == "CONFIRMED":
            if got != "CONFIRMED" or rid not in right:
                fail("identity", tag)
        elif want in ("DOI MISMATCH", "DOI DEAD"):
            if got != want:
                fail("identity", tag + (" (a bad identifier reported as real)" if got in ("CONFIRMED", "MATCHED") else ""))
            elif k.get("suggest") and norm_id(c.get("suggested_id")) != norm_id(k["suggest"]):
                fail("fuzzy", f"{k['find']!r}: suggestion {c.get('suggested_id')} (want {k['suggest']})")
        elif want == "WEB SOURCE":
            if got in ("CONFIRMED", "MATCHED") and rid not in right:
                fail("identity", tag + " (a web page reported as an unrelated registry work)")
        elif want == "UNCONFIRMED":
            if got == "CONFIRMED":
                fail("identity", tag + " (no identifier was cited, nothing can be confirmed)")
            elif got == "MATCHED" and rid not in right and not k.get("any_match_ok"):
                fail("fuzzy", tag + (" (a known wrong record)" if rid in wrong else " (not the cited work)"))
        else:
            fail("identity", f"{k['find']!r}: unknown key level {want}")

    ex = key.get("extraction") or {}
    if extraction == "live" and ex.get("count") is not None and abs(len(cits) - ex["count"]) > ex.get("tolerance", 0):
        fail("extraction", f"{len(cits)} references extracted (key: {ex['count']})")

    ca = key.get("code_availability")
    if ca is not None:
        got = run.get("code_availability")
        if got is None:
            fail("code_avail", "the code under test has no code/data precheck")
        elif bool(got.get("missing_link")) != ca["missing_link"]:
            fail("code_avail", f"missing_link {got.get('missing_link')} (key {ca['missing_link']})")

    cd = key.get("code_digest")
    if cd is not None and "digest" not in skip:
        dig = run.get("digest") or ""
        fetched = bool(dig) and "not fetched" not in dig and "(none" not in dig and "unavailable" not in dig
        if fetched != cd["fetched"]:
            fail("code_digest", f"package fetched={fetched} (key {cd['fetched']})")
        for s in cd.get("must_contain", []):
            if s not in dig:
                fail("code_digest", f"digest lacks {s!r}")
            elif prompt and s not in prompt:
                fail("code_digest", f"the panel prompt lacks {s!r}")

    cp = key.get("compaction") or {}
    if "compaction" not in skip and prompt:
        for s in cp.get("panel_must_not_contain", []):
            if re.search(re.escape(s), prompt, re.I):
                fail("compaction", f"author identifier reaches the panel: {s!r}")
        man = run.get("compaction_manifest")
        if isinstance(man, dict) and man.get("_failure"):
            fail("compaction", f"compaction failed: {man['_failure']}")
        if run.get("full_text_chars") and len(prompt) < cp.get("min_kept_ratio", 0.5) * run["full_text_chars"]:
            fail("compaction", f"panel prompt {len(prompt)} chars vs paper {run['full_text_chars']}")
    elif "compaction" not in skip and not prompt:
        fail("compaction", f"no panel prompt ({run.get('stopped')})")

    ev = key.get("evidence") or {}
    for s in ev.get("must_contain", []):
        if s not in prompt:
            fail("evidence", f"panel prompt lacks {s!r}")
    for s in ev.get("must_not_contain", []):
        if s in prompt:
            fail("evidence", f"panel prompt carries {s!r}")

    # fail closed: nothing below confirmed identity grounds an integrity finding
    confirmed_raw = {c.get("raw") for k, c in keyed if level_of(c) == "CONFIRMED"} | \
                    {c.get("raw") for c in cits if level_of(c) == "CONFIRMED"}
    elig = run.get("misattribution_eligible")
    if elig is None:
        fail("fail_closed", "misattribution selection never ran")
    else:
        for c in elig:
            if c.get("raw") not in confirmed_raw:
                fail("fail_closed", f"misattribution would judge a {level_of(c)} reference: {(c.get('raw') or '')[:70]!r}")
    cs = (run.get("citations") or {}).get("citestamp") or {}
    confirmed_ids = {record_id_of(c) for c in cits if level_of(c) == "CONFIRMED"}
    for row in cs.get("results") or []:
        if norm_id(row.get("doi")) not in confirmed_ids:
            fail("fail_closed", f"CiteStamp queried an unconfirmed DOI {row.get('doi')}")
    for c in cits:
        if level_of(c) == "MATCHED":
            rid = record_id_of(c)
            if rid and rid in prompt.lower():
                fail("fail_closed", f"the panel sees the unconfirmed candidate {rid} for {(c.get('raw') or '')[:60]!r}")

    hard = ("identity", "extraction", "code_avail", "code_digest", "compaction", "evidence")
    need = list(hard) + (["fail_closed"] if bar >= 2 else []) + (["fuzzy"] if bar >= 3 else [])
    green = all(not groups[g] for g in need)
    return {"green": green, "bar": bar, "groups": groups, "levels": {
        k["find"]: f"{level_of(c)} {record_id_of(c)}".strip() for k, c in keyed}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", default=os.environ.get("ICSAC_REPLAY_CORPUS"))
    ap.add_argument("--paper", action="append", help="corpus subdirectory (repeatable; default all)")
    ap.add_argument("--code", default=str(DEFAULT_CODE), help="pipeline checkout to replay")
    ap.add_argument("--extraction", choices=("frozen", "live"), default="frozen")
    ap.add_argument("--skip", default="", help=f"comma list of {','.join(STAGES)}")
    ap.add_argument("--bar", type=int, default=1, choices=(1, 2, 3))
    ap.add_argument("--keep", action="store_true", help="keep the shadow directory")
    ap.add_argument("--out", help="write the full result JSON here")
    a = ap.parse_args()
    if not a.corpus or not Path(a.corpus).is_dir():
        print("replay: no corpus (set ICSAC_REPLAY_CORPUS or --corpus)", file=sys.stderr)
        return 2
    skip = {s for s in a.skip.split(",") if s}
    corpus = Path(a.corpus).expanduser()
    papers = a.paper or sorted(p.name for p in corpus.iterdir() if (p / "key.json").exists())
    code_dir = Path(a.code).expanduser().resolve()
    results, all_green = {}, True
    for name in papers:
        pdir = corpus / name
        key = json.loads((pdir / "key.json").read_text())
        shadow = Path(tempfile.mkdtemp(prefix="icsac-replay-"))
        cwd = os.getcwd()
        try:
            os.chdir(code_dir)
            run = run_paper(pdir, code_dir, a.extraction, skip, shadow, log=print)
            res = grade(key, run, a.bar, skip, a.extraction)
            res["stages_s"] = run["stages"]
            res["notifications"] = run["notifications"]
            res["builder"] = run.get("builder")
            if run.get("aggregate"):
                man = run.get("compaction_manifest") if isinstance(run.get("compaction_manifest"), dict) else {}
                res["stopped_early"] = f"{run['aggregate'].get('recommendation')}: {run['aggregate'].get('reason')}" + (
                    f" [compaction: {man.get('_failure')}]" if man.get("_failure") else "")
            results[name] = res
            if a.out:
                res["run"] = {k: run.get(k) for k in ("stopped", "aggregate", "compaction_manifest", "code_availability",
                                                      "misattribution_eligible", "citations")}
        except Exception as exc:
            results[name] = {"green": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            os.chdir(cwd)
            if a.keep:
                print(f"  shadow kept: {shadow}")
            else:
                shutil.rmtree(shadow, ignore_errors=True)
        r = results[name]
        all_green &= bool(r.get("green"))
        print(f"{'GREEN' if r.get('green') else 'RED  '} {name}  (code {code_dir.name}, bar {a.bar}, "
              f"extraction {a.extraction}, skip {','.join(sorted(skip)) or '-'})")
        if r.get("error"):
            print(f"    error: {r['error']}")
        if r.get("stopped_early"):
            print(f"    stopped early: {r['stopped_early']}")
        for g, msgs in (r.get("groups") or {}).items():
            for m in msgs:
                print(f"    [{g}] {m}")
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1, default=str))
    return 0 if all_green else 1


if __name__ == "__main__":
    sys.exit(main())
