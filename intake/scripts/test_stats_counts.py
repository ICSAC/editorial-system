#!/usr/bin/env python3
"""Regression checks for the /stats snapshot counts (2026-11-03).
Offline: no network, no mail; every write lands in one temporary directory that
is removed at the end, pass or fail. Run from the repo root:
    .venv/bin/python intake/scripts/test_stats_counts.py

Covers: a code-package digest and any other markdown without review frontmatter
never count as panel reviews; the curation team's verdicts count one per
submission round, add the papers accepted before the submission form existed,
count a paper that has both a folder and a published copy once, skip test
submissions and undecided ones, and are left out (never zero) when the
submission folders cannot be read.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import stats  # noqa: E402

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def review(rec: str, rid: str, doi: str = "") -> str:
    head = f"---\nrecord_id: {rid}\n" + (f'doi: "{doi}"\n' if doi else "")
    return head + f"review_date: 2026-10-20T10:00:00Z\nrecommendation: {rec}\ndisagreement: False\n---\n\n### Aggregate scores\n"


def folder(root: Path, sid: str, decision: str | None, doi: str = "") -> None:
    d = root / sid
    d.mkdir()
    (d / "submission.json").write_text(json.dumps({"doi": doi} if doi else {}))
    (d / "state.json").write_text(json.dumps({"state": "completed", "decision": decision} if decision else {"state": "awaiting_decision"}))


with tempfile.TemporaryDirectory(prefix="stats-counts-") as td:
    tmp = Path(td)
    rdir = tmp / "reviews"; rdir.mkdir()
    pub = tmp / "public-reviews"; pub.mkdir()
    subs = tmp / "submissions"; subs.mkdir()

    # ── what counts as a panel review ───────────────────────────────────────
    (rdir / "ICSAC-SUB-90001_a-paper.md").write_text(review("REVISE_AND_RESUBMIT", "ICSAC-SUB-90001"))
    (rdir / "ICSAC-SUB-90001_code_digest.md").write_text("# Code package digest\n\nFetched read-only.\n")
    (rdir / "ICSAC-SUB-90001_citations.md").write_text("# Citations\n")
    (rdir / "ICSAC-SUB-90001_notes.md").write_text("A note with no frontmatter.\n")
    (rdir / "ICSAC-SUB-90002_b-paper.md").write_text(review("RECOMMEND", "ICSAC-SUB-90002"))
    (rdir / "ICSAC-SUB-90003_c-paper.md").write_text(review("REJECT", "ICSAC-SUB-90003"))
    st = stats.compute_stats(str(rdir))
    check(st["total_reviewed"] == 3, f"digests, citation files and frontmatter-less notes are not reviews ({st['total_reviewed']})")
    check(st["recommendation_mix"]["REVIEW_FURTHER"] == 0, "a digest never reads as a REVIEW_FURTHER review")
    check("curation_verdicts" not in st, "no submission folders given: no verdict table")

    # ── verdicts, one per submission round ──────────────────────────────────
    folder(subs, "ICSAC-SUB-90001", "revise")
    folder(subs, "ICSAC-SUB-90002", "accept", doi="10.5281/zenodo.222")
    folder(subs, "ICSAC-SUB-90003", "scope_reject")
    folder(subs, "ICSAC-SUB-90004", None)                       # undecided
    folder(subs, "ICSAC-SUB-90005", "accept")                   # a revised round, accepted
    (subs / "test").mkdir()
    folder(subs / "test", "ICSAC-SUB-TEST-1", "accept")
    folder(subs, "ICSAC-SUB-TEST-2", "accept")
    (subs / "queue").mkdir()
    # published copies: a pre-form accepted record, the same paper twice under
    # its folder's DOI, and a folder's own slug copy
    (pub / "111.md").write_text(review("RECOMMEND", "111", doi="10.5281/zenodo.111"))
    (pub / "222.md").write_text(review("RECOMMEND", "222", doi="https://doi.org/10.5281/ZENODO.222"))
    (pub / "b-paper.md").write_text(review("RECOMMEND", "ICSAC-SUB-90002", doi="10.5281/zenodo.222"))
    (pub / "111_review_quality_control.md").write_text("---\nrecord_id: 111\nreview_quality_control_flag: false\n---\n")
    v = stats.compute_stats(str(rdir), str(pub), str(subs)).get("curation_verdicts")
    check(v == {"accept": 3, "revise": 1, "scope_reject": 1}, f"one verdict per round, pre-form accepts added, the same paper once ({v})")

    folder(subs, "ICSAC-SUB-90006", "withdrawn")
    v = stats.compute_stats(str(rdir), str(pub), str(subs)).get("curation_verdicts")
    check(v.get("other") == 1, f"a verdict outside the three is counted, not dropped ({v})")

    (subs / "ICSAC-SUB-90006" / "state.json").write_text("{ not json")
    st = stats.compute_stats(str(rdir), str(pub), str(subs))
    check("curation_verdicts" not in st, "an unreadable state.json leaves the table out instead of undercounting")
    check(st["total_reviewed"] == stats.compute_stats(str(rdir), str(pub))["total_reviewed"] == 5,
          "the panel figures still compute when the verdicts cannot")

    missing = stats.compute_stats(str(rdir), str(pub), str(tmp / "nowhere"))
    check("curation_verdicts" not in missing, "a missing submissions folder leaves the table out")

    # ── the writers carry the verdicts by default ───────────────────────────
    (subs / "ICSAC-SUB-90006" / "state.json").write_text(json.dumps({"decision": "accept"}))
    os.environ["ICSAC_SUBMISSIONS_ROOT"] = str(subs)
    out = tmp / "site" / "stats.json"
    stats.write_stats(str(rdir), str(out), str(pub))
    w = json.loads(out.read_text())
    check(w.get("curation_verdicts", {}).get("accept") == 4, f"write_stats reads the default submissions folder ({w.get('curation_verdicts')})")

print(f"\n{'ALL GREEN' if not failures else f'{len(failures)} FAILURE(S)'}")
sys.exit(1 if failures else 0)
