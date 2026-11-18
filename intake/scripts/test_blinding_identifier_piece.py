#!/usr/bin/env python3
"""Blinding does not withhold a paper over a piece of an identifier it already
removed. The extraction can return an ORCID iD that PDF text wraps at a line
break twice: whole ("0000-" + line break + "0002-1825-0097") and as the part
after the break. The whole iD is removed from every place it occurs, so the
part then matches nothing, and compaction used to fail closed on it as an
unmatched identity span. Now a snippet that is a piece of one already removed
in its category, and of which nothing is left in the text in any spacing or
dash form, is recorded as covered. A piece still present in another form, an
identifier the text never held, and a piece under four characters still fail
closed. Every fixture is synthetic (the iD is ORCID's published example record).

    .venv/bin/python intake/scripts/test_blinding_identifier_piece.py
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
PIECE = "0002-1825-0097"
KNOWN = {"names": ["A. Example"], "other": [ORCID, "a.example@example.com"]}


def paper(tail: str = "") -> str:
    return ("A Paper\nA. Example\nExample University\n\n" + "Body text. " * 80
            + f"\n* Corresponding author. Email: a.example@example.com. ORCID: {WRAPPED}.\n" + tail)


def compact(text: str, spans: dict):
    orig = rc._claude_call
    rc._claude_call = lambda *a, **k: (json.dumps(spans), "", 0)
    try:
        return rc.compact_paper(text, log=lambda *_: None, known_identity=KNOWN)
    finally:
        rc._claude_call = orig


def spans(orcids: list[str], names: list[str] | None = None) -> dict:
    return {"author_names": names or ["A. Example"], "affiliations": ["Example University"],
            "emails": ["a.example@example.com"], "orcids": orcids}


print("1. the iD returned whole and as the part after its line break")
redacted, manifest = compact(paper(), spans([WRAPPED, PIECE]))
check(not manifest.get("_failure"), f"no compaction failure: {manifest.get('_failure')!r}")
check(bool(redacted) and "1825" not in redacted and "0097" not in redacted,
      "the panel's manuscript carries no iD digits")
check(manifest.get("orcids") == [WRAPPED], f"the manifest names the removed iD: {manifest.get('orcids')!r}")
check([c.get("snippet") for c in manifest.get("covered_pieces") or []] == [PIECE],
      f"the piece is recorded as covered: {manifest.get('covered_pieces')!r}")
check(not manifest.get("match_failures"), f"no match failure: {manifest.get('match_failures')!r}")

print("2. the order the extraction lists them in does not matter")
redacted, manifest = compact(paper(), spans([PIECE, WRAPPED]))
check(not manifest.get("_failure") and bool(redacted) and "1825" not in redacted,
      f"piece first, then the whole: {manifest.get('_failure')!r}")

print("3. a name returned twice, the second with other spacing")
redacted, manifest = compact(paper(), spans([WRAPPED], names=["A. Example", "A.  Example"]))
check(not manifest.get("_failure") and bool(redacted),
      f"the second copy of a removed name is covered: {manifest.get('_failure')!r}")

print("4. still fails closed")
redacted, manifest = compact(paper("Appendix contact: 0002 – 1825 – 0097.\n"), spans([WRAPPED, PIECE]))
check(manifest.get("_failure") == "identity span unmatched" and redacted == "",
      "a piece still in the text in another form")
redacted, manifest = compact(paper(), spans([WRAPPED, "0000-0002-1825-0098"]))
check(manifest.get("_failure") == "identity span unmatched" and redacted == "",
      "an iD the text never held")
redacted, manifest = compact(paper(), spans([WRAPPED, "097"]))
check(manifest.get("_failure") == "identity span unmatched" and redacted == "",
      "a piece under four characters")
text, man, failed = rc._apply_removals("Body. Contact x.y@example.org.", {"emails": ["q.r@example.org"]})
check(failed and not man.get("covered_pieces"), "an email that was never there, with nothing removed")

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
