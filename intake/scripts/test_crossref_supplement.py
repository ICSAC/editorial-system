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
art = x[x.find("<journal_article"):]   # journal_metadata may carry the journal title DOI's own doi_data
check(0 < art.find("<rel:program") < art.find("<doi_data"), "the relation sits before the article's doi_data")
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

print("3. the journal title DOI (no ISSN yet)")
import config  # noqa: E402
saved = getattr(config, "CROSSREF_JOURNAL_DOI", None)
try:
    config.CROSSREF_JOURNAL_DOI = ""
    x = build(dict(BASE))
    jm = x[x.find("<journal_metadata"):x.find("</journal_metadata>")]
    check(bool(x) and "<doi_data" not in jm, "unset: journal_metadata carries no doi_data (the deposit as before)")
    config.CROSSREF_JOURNAL_DOI = "10.67697/persistence"
    x = build(dict(BASE, code_data={"available": True, "url": ZEN}))
    jm = x[x.find("<journal_metadata"):x.find("</journal_metadata>")]
    check(bool(x) and "<doi>10.67697/persistence</doi>" in jm and "<resource>https://icsacinstitute.org/journal/</resource>" in jm,
          "set: journal_metadata carries the title DOI and the journal page, and validates")
    check(x.count("<doi>10.67697/icsac.2026.099</doi>") == 1, "the article keeps its own DOI")
finally:
    if saved is None:
        delattr(config, "CROSSREF_JOURNAL_DOI") if hasattr(config, "CROSSREF_JOURNAL_DOI") else None
    else:
        config.CROSSREF_JOURNAL_DOI = saved

print("4. the Zenodo archive copy follows the Crossref record")
import repository_deposit as rd  # noqa: E402
sub = dict(BASE, resource_type="preprint", publication_date="2026-09-29")
md = rd._build_metadata(sub, external_doi="10.67697/icsac.2026.099")
check(md.get("upload_type") == "publication" and md.get("publication_type") == "article",
      "with an ICSAC DOI, a form 'preprint' becomes a journal article")
check(md.get("journal_title") == getattr(config, "CROSSREF_JOURNAL_TITLE", "Persistence")
      and md.get("journal_volume") == str(getattr(config, "CROSSREF_JOURNAL_VOLUME", "1")),
      "it names the journal and the volume from config")
check(md.get("doi") == "10.67697/icsac.2026.099", "the external DOI still rides on the draft")
md = rd._build_metadata(dict(sub, resource_type="dataset"), external_doi="10.67697/icsac.2026.099")
check(md.get("upload_type") == "publication" and md.get("publication_type") == "article",
      "any form type is overridden once the work is an ICSAC-registered article")
md = rd._build_metadata(sub)
check(md.get("publication_type") == "preprint" and "journal_title" not in md,
      "without an ICSAC DOI (Zenodo mints its own), the author's type stands")
saved_ct = getattr(config, "CROSSREF_CONTENT_TYPE", None)
try:
    config.CROSSREF_CONTENT_TYPE = "report-paper"
    md = rd._build_metadata(sub, external_doi="10.67697/icsac.2026.099")
    check(md.get("publication_type") == "report" and "journal_title" not in md,
          "a report-paper deployment archives a report, with no journal fields")
finally:
    if saved_ct is None:
        delattr(config, "CROSSREF_CONTENT_TYPE")
    else:
        config.CROSSREF_CONTENT_TYPE = saved_ct

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
