#!/usr/bin/env python3
"""The author-facing compaction disclosure states only what was measured. The
reference-entry count is a heuristic that undercounts author-year lists, so the
letter names the removed references list by its size, never by an entry count.
Offline, no files written. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_disclosure_wording.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import review_compaction as rc  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


man = {"author_names": ["A. Author"], "references_count": 2, "references_section_chars": 6430}
out = rc.render_manifest(man)
ref = [line for line in out.splitlines() if "References list" in line]
check(len(ref) == 1, "one references line")
check(bool(ref) and "6430 characters" in ref[0], "the line gives the size of the removed list")
check(bool(ref) and "item" not in ref[0], "the line carries no entry count")

out0 = rc.render_manifest({"author_names": ["A. Author"]})
check("References list" not in out0, "no references line when no list was removed")

sys.exit(1 if failures else 0)
