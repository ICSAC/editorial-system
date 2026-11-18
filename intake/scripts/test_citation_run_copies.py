#!/usr/bin/env python3
"""Each run's citation list is kept.

A re-run of the citation phase overwrote <record_id>_citations.json/.md, so
two runs on the same PDF could not be compared item by item. Each run's final
files are now copied to reviews/citation-runs/<record_id>/<UTC stamp>_*; the
live names stay the latest run; a run that writes no list copies nothing.
Offline: extraction, verification and the later phases are stubbed.

    .venv/bin/python intake/scripts/test_citation_run_copies.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import citation_verify as cv  # noqa: E402
import review  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


RUN = {"cites": []}
cv.extract_citations = lambda text, rid: [dict(c) for c in RUN["cites"]]
cv.verify_all = lambda cits: [dict(c, verified=True, confidence="exact-id", doi_identity="confirmed") for c in cits]
review._run_citation_misattribution = lambda rid, cits, text, report: report
review._run_citestamp_check = lambda rid, cits, report: report
rid = "ICSAC-SUB-TEST-1790500001"
data = {"record_id": rid, "full_text": "x " * 200}


def cite(i):
    return {"raw": f"Author{i}, A. (2001). Work {i}.", "authors": [f"A. Author{i}"], "year": 2001,
            "title": f"Work number {i}", "doi": f"10.0000/w.{i}", "arxiv_id": None, "type": "doi", "claim_context": ""}


with tempfile.TemporaryDirectory(prefix="icsac-citruns-") as td:
    config.REVIEWS_DIR = td
    runs = Path(td) / "citation-runs" / rid

    print("[1] two runs, two kept lists")
    RUN["cites"] = [cite(1), cite(2), cite(3)]
    review._run_citation_verify(data)
    RUN["cites"] = [cite(1), cite(2)]
    review._run_citation_verify(data)
    kept = sorted(p.name for p in runs.glob("*_citations.json")) if runs.exists() else []
    check(len(kept) == 2, f"two run copies ({kept})")
    counts = [len(json.loads((runs / k).read_text())["citations"]) for k in kept]
    check(counts == [3, 2], f"each copy is its own run's list ({counts})")
    check(len(sorted(runs.glob("*_citations.md"))) == 2, "the rendered reports are kept too")
    live = json.loads((Path(td) / f"{rid}_citations.json").read_text())
    check(len(live["citations"]) == 2, "the live file is the latest run")

    print("[2] a run that writes no list copies nothing")
    RUN["cites"] = []
    review._run_citation_verify(data)
    check(len(list(runs.glob("*_citations.json"))) == 2, "no third copy of the earlier list")

    print("[3] nothing that lists reviews/ sees the copies")
    top_json = sorted(p.name for p in Path(td).iterdir() if p.is_file() and p.name.endswith("_citations.json"))
    top_md = sorted(p.name for p in Path(td).iterdir() if p.is_file() and p.name.endswith("_citations.md"))
    check(top_json == [f"{rid}_citations.json"] and top_md == [f"{rid}_citations.md"],
          f"only the live pair sits in reviews/ ({top_json}, {top_md})")

    print("[4] a run whose save fails copies nothing, even when later phases touch the old file")
    real_save = cv.save_citation_report

    def _fail_save(*a, **k):
        raise OSError("disk full")
    cv.save_citation_report = _fail_save
    review._run_citestamp_check = lambda r, cits, report: (Path(td) / f"{rid}_citations.json").touch() or report
    RUN["cites"] = [cite(7)]
    review._run_citation_verify(data)
    cv.save_citation_report = real_save
    check(len(list(runs.glob("*_citations.json"))) == 2, "the earlier run's list is not copied as this run")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
