#!/usr/bin/env python3
"""Blinding reads an ORCID iD that PDF text extraction split at a line break:
the hyphen at the break dropped ("0000-" + "0002-1825-0097" read as
"00000002-1825-0097"), a line break after it, or Unicode dashes. The known iD
is removed from the manuscript, the pattern sweep removes it from every text
the panel reads, and the anonymized-at-submission check sees it. Runs of
figures that are not iDs are left alone. Every fixture is synthetic (the iD is
ORCID's published example record)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


import review_compaction as rc  # noqa: E402

ORCID = "0000-0002-1825-0097"
SPLIT_FORMS = {
    "hyphen dropped at the break": "00000002-1825-0097",
    "line break after the hyphen": "0000-\n0002-1825-0097",
    "Unicode hyphens": "0000‐0002‐1825‐0097",
    "space after the hyphen": "0000- 0002-1825-0097",
    "every hyphen dropped": "0000000218250097",
}

# ── 1. the known iD is removed from the manuscript in every split form ─────
print("1. known iD, manuscript")
for label, form in SPLIT_FORMS.items():
    text = f"A Paper\nA. Example\n\nBody.\n* Corresponding author. ORCID: {form}.\n"
    out, manifest, failed = rc._apply_removals(text, {"author_names": ["A. Example"], "orcids": [ORCID]})
    check(not [f for f in failed if f["category"] == "orcids"], f"{label}: no unmatched iD")
    check("1825" not in out and "0097" not in out, f"{label}: no iD digits left: {out!r}")
    check(manifest.get("orcids") == [ORCID], f"{label}: the manifest names the iD once")

text = f"ORCID: {ORCID}\n\nBody.\n* Corresponding author. ORCID: 00000002-1825-0097.\n"
out, manifest, failed = rc._apply_removals(text, {"orcids": [ORCID]})
check("1825" not in out, f"a verbatim copy and a split copy are both removed: {out!r}")
check(manifest.get("orcids") == [ORCID] and not failed, "removed once in the manifest, no failure")

out, manifest, failed = rc._apply_removals("ORCID: 0000-0002-1825-0098.", {"orcids": [ORCID]})
check("0098" in out and failed, "a different iD is neither removed nor reported as this one")

# ── 2. compaction end to end: the paper reaches the panel, blinded ─────────
print("2. compact_paper")


def _fake_claude(spans):
    return lambda text, *a, **k: (json.dumps(spans), "", 0)


paper = ("A Paper\nA. Example\nExample University\n\n" + "Body text. " * 80
         + "\n* Corresponding author. Email: a.example@example.com. ORCID: 0000-\n0002-1825-0097.\n")
orig_call = rc._claude_call
rc._claude_call = _fake_claude({"author_names": ["A. Example"], "affiliations": ["Example University"],
                                "emails": ["a.example@example.com"], "orcids": [ORCID]})
try:
    redacted, manifest = rc.compact_paper(paper, log=lambda *_: None,
                                          known_identity={"names": ["A. Example"], "other": [ORCID]})
finally:
    rc._claude_call = orig_call
check(not manifest.get("_failure"), f"no compaction failure: {manifest.get('_failure')!r}")
check(bool(redacted) and "1825" not in redacted, "the redacted manuscript carries no iD digits")


def _compact(text, spans, known):
    orig = rc._claude_call
    rc._claude_call = _fake_claude(spans)
    try:
        return rc.compact_paper(text, log=lambda *_: None, known_identity=known)
    finally:
        rc._claude_call = orig


known = {"names": ["A. Example"], "other": [ORCID, "a.example@example.com"]}
for label, form in {**SPLIT_FORMS, "line break, no hyphen": "0000\n0002-1825-0097",
                    "canonical": ORCID}.items():
    text = f"A Paper\nA. Example\n\n" + "Body text. " * 80 + f"\n* ORCID: {form}.\n"
    redacted, manifest = _compact(text, {"author_names": ["A. Example"]}, known)
    check(not manifest.get("_failure") and "1825" not in redacted,
          f"the submission's own iD is removed when the extraction omits it ({label})")
check(manifest.get("known_orcids") == [ORCID], f"the manifest carries the known iD: {manifest.get('known_orcids')!r}")
aux = rc.blind_aux_text("Abstract. Contact ORCID 0000 0002 1825 0097 or 0000\n0002-1825-0097.", manifest)
check("1825" not in aux, f"the other panel texts lose the known iD in any form: {aux!r}")

text = "A Paper\nA. Example\n\n" + "Body text. " * 80
redacted, manifest = _compact(text, {"author_names": ["A. Example"], "orcids": [ORCID]}, known)
check(manifest.get("_failure") == "identity span unmatched" and redacted == "",
      "an iD the extraction reports but the text lacks in any form still fails closed")

# ── 3. the pattern sweep over every panel-facing text ──────────────────────
print("3. blind_aux_text sweep")
for label, form in SPLIT_FORMS.items():
    out = rc.blind_aux_text(f"Contact: ORCID {form}; see the appendix.", {})
    check("1825" not in out and "[orcid withheld]" in out, f"{label}: swept with no manifest: {out!r}")
out = rc.blind_aux_text("ORCID 0000 0002 1825 0097", {"orcids": [ORCID]})
check("1825" not in out, f"a known iD with spaces between groups is removed: {out!r}")
for keep in ("Years 2019 2020 2021 2022 were pooled.", "Record 1234567890123456 was kept.",
             "Sample 0001 0002 0003 0004 in the table.", "ISSN 0000-0002, volume 12.",
             "https://doi.org/10.5281/zenodo.12345678", "Grant 2021-00021825-0097-A."):
    check(rc.blind_aux_text(keep, {}) == keep, f"left alone: {keep!r}")

# ── 4. anonymized-at-submission check ──────────────────────────────────────
print("4. identity_in_text")
for label, form in SPLIT_FORMS.items():
    hits = rc.identity_in_text(f"Body. ORCID: {form}.", {"names": [], "other": [ORCID]})
    check(hits == [ORCID], f"{label}: the split iD counts as present: {hits!r}")
check(rc.identity_in_text("Body only.", {"names": [], "other": [ORCID]}) == [], "absent iD is not found")

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
