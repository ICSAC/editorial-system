#!/usr/bin/env python3
"""Checks for the code and data relation in the Crossref deposit (2026-09-29):
an article whose author gave a public code and data link carries
isSupplementedBy in its Crossref record, as a DOI when the link is a doi.org
link and as a URI otherwise; every variant validates against the Crossref
5.4.0 schema. Offline and pure: no network, no files written.
Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_crossref_supplement.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


import crossref_deposit as cd  # noqa: E402

BASE = {"sub_id": "ICSAC-SUB-00099", "title": "A test paper", "abstract": "An abstract.",
        "license": "cc-by-4.0", "source": "upload",
        "creators": [{"name": "Test, Author", "orcid": "0000-0002-1825-0097"}]}
ZEN = "https://doi.org/10.5281/zenodo.23042786"


def build(sub: dict, content_type: str = "journal-article") -> str:
    xml = cd.build_deposit_xml(sub, doi="10.67697/icsac.2026.099",
                               landing_url="https://icsacinstitute.org/publications/a-test-paper",
                               pdf_url="https://icsacinstitute.org/papers/ICSAC-SUB-00099.pdf",
                               content_type=content_type)
    try:
        cd.validate_xml(xml)
        ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"       schema: {exc}")
        ok = False
    return xml.decode() if ok else ""


print("1. which identifier")
rel = cd.code_data_relation
check(rel(dict(BASE, code_data={"available": True, "url": ZEN})) == ("doi", "10.5281/zenodo.23042786"),
      "a doi.org link is registered as its DOI")
check(rel(dict(BASE, code_data={"available": True, "url": "https://dx.doi.org/10.5061/dryad.abc123"})) == ("doi", "10.5061/dryad.abc123"),
      "a dx.doi.org link too")
check(rel(dict(BASE, code_data={"available": True, "url": "https://github.com/someone/repo"})) == ("uri", "https://github.com/someone/repo"),
      "any other public link is registered as a URI")
check(rel(dict(BASE, code_data={"available": False, "url": None})) is None, "no link, no relation")
check(rel(dict(BASE)) is None, "a submission from before the field existed has no relation")
check(rel(dict(BASE, code_data={"available": True, "url": "  "})) is None, "a blank link is no link")

print("2. the deposit")
x = build(dict(BASE, code_data={"available": True, "url": ZEN}))
check(bool(x), "an article with a DOI archive validates against the Crossref 5.4.0 schema")
check('<rel:inter_work_relation relationship-type="isSupplementedBy" identifier-type="doi">10.5281/zenodo.23042786</rel:inter_work_relation>' in x,
      "it carries isSupplementedBy -> the archive DOI")
check(x.find("<rel:program") < x.find("<doi_data") and x.find("<rel:program") > 0, "the relation sits before doi_data")
check("hasPreprint" not in x, "an upload with no preprint carries no preprint relation")

x = build(dict(BASE, code_data={"available": True, "url": "https://github.com/someone/repo"}))
check(bool(x) and 'identifier-type="uri">https://github.com/someone/repo<' in x,
      "a GitHub link validates and is registered as a URI")

x = build(dict(BASE, source="doi", doi="10.5281/zenodo.123456",
               code_data={"available": True, "url": ZEN}))
check(bool(x) and x.count("<rel:program") == 1 and x.count("<rel:related_item>") == 2,
      "a preprint and an archive share one relations program, two related items")
check('relationship-type="hasPreprint"' in x and 'relationship-type="isSupplementedBy"' in x,
      "both relations present")

x = build(dict(BASE))
check(bool(x) and "<rel:program" not in x, "no preprint and no archive: no relations program at all")

x = build(dict(BASE, code_data={"available": True, "url": ZEN}), content_type="report-paper")
check(bool(x) and "isSupplementedBy" in x, "report-paper deposits carry the relation and validate too")

x = build(dict(BASE, code_data={"available": True, "url": "https://example.org/data?x=1&y=2"}))
check(bool(x) and "x=1&amp;y=2" in x, "a link with an ampersand is escaped and still validates")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
