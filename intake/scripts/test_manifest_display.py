#!/usr/bin/env python3
"""The decision letter lists what blinding removed, one line per category, and
an entry the PDF text wrapped across lines reads as one line there. An ORCID iD
wrapped after a hyphen ("0000-" + line break + "0002-1825-0097") is listed as
0000-0002-1825-0097, a name or affiliation wrapped at a space keeps one space,
and a hyphenated name wrapped at its hyphen keeps the hyphen. The manifest
itself keeps the exact text that was removed. Every fixture is synthetic (the
iD is ORCID's published example record).

    .venv/bin/python intake/scripts/test_manifest_display.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import review_compaction as rc  # noqa: E402

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


ORCID = "0000-0002-1825-0097"
WRAPPED = "0000-\n0002-1825-0097"


def entry_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip() and not line.startswith("  Total")]


print("1. entries the PDF text wrapped")
out = rc.render_manifest({"author_names": ["A.\nExample", "Jean-\nPaul Example"],
                          "affiliations": ["Example\nUniversity, Example City"],
                          "emails": ["a.example@example.com"],
                          "orcids": [WRAPPED]})
check(all(line.startswith("  - ") for line in entry_lines(out)), f"every category is one line: {out!r}")
check(f"  - ORCID iDs: {ORCID}" in out.splitlines(), "the wrapped iD is listed whole")
check("  - Author names: A. Example, Jean-Paul Example" in out.splitlines(),
      "a name keeps one space, a hyphenated name its hyphen")
check("  - Affiliations: Example University, Example City" in out.splitlines(),
      "an affiliation wrapped at a space keeps one space")
out = rc.render_manifest({"orcids": ["0000- \n 0002-1825-0097", "0000-0002-1825-0097\n"]})
check(f"  - ORCID iDs: {ORCID}, {ORCID}" in out.splitlines(),
      f"spaces around the break and a trailing break: {out!r}")

print("2. text that was never wrapped is left as it was")
out = rc.render_manifest({"author_names": ["A. Example"], "affiliations": ["Institute X - Branch Y"],
                          "emails": ["a.example@example.com"], "orcids": [ORCID]})
check(out.splitlines()[:4] == ["  - Author names: A. Example",
                               "  - Affiliations: Institute X - Branch Y",
                               "  - Email addresses: a.example@example.com",
                               f"  - ORCID iDs: {ORCID}"], f"unchanged: {out!r}")

print("3. a paper whose iD wraps, end to end")
paper = ("A Paper\nA. Example\nExample University\n\n" + "Body text. " * 80
         + f"\n* Corresponding author. Email: a.example@example.com. ORCID: {WRAPPED}.\n")
spans = {"author_names": ["A. Example"], "affiliations": ["Example University"],
         "emails": ["a.example@example.com"], "orcids": [WRAPPED]}
orig = rc._claude_call
rc._claude_call = lambda *a, **k: (json.dumps(spans), "", 0)
try:
    redacted, manifest = rc.compact_paper(paper, log=lambda *_: None,
                                          known_identity={"names": ["A. Example"],
                                                          "other": [ORCID, "a.example@example.com"]})
finally:
    rc._claude_call = orig
check(not manifest.get("_failure") and "1825" not in redacted, f"blinded: {manifest.get('_failure')!r}")
check(manifest.get("orcids") == [WRAPPED], "the manifest keeps the exact text removed")
out = rc.render_manifest(manifest)
check(f"  - ORCID iDs: {ORCID}" in out.splitlines(), f"the letter lists the iD on one line: {out!r}")
check(all(line.startswith("  - ") for line in entry_lines(out)), "no category spills onto a second line")

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
