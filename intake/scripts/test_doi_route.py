#!/usr/bin/env python3
"""Checks for the DOI route as a Persistence article (policy 2026-09-28): a
paper submitted by its preprint DOI gets its own DOI under the Institute's
prefix, linked to the preprint by a hasPreprint relation. Offline: no
network, no mail; every write lands in one temporary directory that is
removed at the end, pass or fail. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_doi_route.py
"""
from __future__ import annotations

import email as _email
import email.policy as _policy
import pathlib
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


def eml_text(p: Path) -> str:
    msg = _email.message_from_bytes(p.read_bytes(), policy=_policy.default)
    return "".join(part.get_payload(decode=True).decode("utf-8", "replace")
                   for part in msg.walk() if part.get_content_type() == "text/plain")


tmp = Path(tempfile.mkdtemp(prefix="doiroute-"))
real_home = pathlib.Path.home
try:
    import crossref_deposit as cd
    import publications

    print("1. the preprint's DOI")
    pd = cd.preprint_doi
    check(pd({"source": "doi", "doi": "10.5281/zenodo.20211868"}) == "10.5281/zenodo.20211868", "Zenodo DOI kept")
    check(pd({"source": "doi", "doi": "2401.12345v2"}) == "10.48550/arXiv.2401.12345", "arXiv id -> arXiv DOI, version dropped")
    check(pd({"source": "doi", "doi": "10.48550/arXiv.2401.12345"}) == "10.48550/arXiv.2401.12345", "arXiv DOI kept")
    check(pd({"source": "doi", "doi": "https://doi.org/10.5281/zenodo.1"}) == "10.5281/zenodo.1", "resolver prefix stripped")
    check(pd({"source": "upload", "doi": ""}) is None, "upload route has no preprint")
    check(pd({"source": "doi", "doi": "not a doi"}) is None, "garbage is no preprint")

    print("2. the deposit")
    base = {"sub_id": "ICSAC-SUB-00099", "title": "A test paper", "abstract": "An abstract.",
            "license": "cc-by-4.0", "creators": [{"name": "Test, Author", "orcid": "0000-0002-1825-0097"}]}
    doi_sub = dict(base, source="doi", doi="10.5281/zenodo.123456")
    xml = cd.build_deposit_xml(doi_sub, doi="10.67697/icsac.2026.099",
                               landing_url="https://icsacinstitute.org/publications/a-test-paper",
                               pdf_url="https://icsacinstitute.org/papers/ICSAC-SUB-00099.pdf",
                               content_type="journal-article")
    try:
        cd.validate_xml(xml)
        check(True, "DOI-route deposit validates against the Crossref 5.4.0 schema")
    except Exception as exc:
        check(False, f"DOI-route deposit validates against the Crossref 5.4.0 schema ({exc})")
    x = xml.decode()
    check('relationship-type="hasPreprint"' in x and ">10.5281/zenodo.123456<" in x,
          "the deposit links the preprint (hasPreprint)")
    check(x.find("<rel:program") < x.find("<doi_data"), "the relation sits before doi_data")
    up = cd.build_deposit_xml(dict(base, source="upload"), doi="10.67697/icsac.2026.098",
                              landing_url="https://icsacinstitute.org/publications/x", pdf_url=None,
                              content_type="journal-article")
    cd.validate_xml(up)
    check(b"hasPreprint" not in up, "an upload deposit carries no preprint relation")
    rp = cd.build_deposit_xml(doi_sub, doi="10.67697/icsac.2026.097",
                              landing_url="https://icsacinstitute.org/publications/y", pdf_url=None,
                              content_type="report-paper")
    try:
        cd.validate_xml(rp)
        check(b"hasPreprint" in rp, "report-paper deposits carry the relation and validate too")
    except Exception as exc:
        check(False, f"report-paper deposits carry the relation and validate too ({exc})")
    check("preprint_doi" in publications._OPTIONAL_KEYS, "the registry keeps preprint_doi")

    print("3. the emails")
    from intake import notify_author as na
    pathlib.Path.home = classmethod(lambda cls: tmp / "home")
    common = dict(to="author@example.com", title="A test paper", author_name="Test Author",
                  verdict="accept", panel_report_md="", rqc_md="", tier=2,
                  compaction_manifest={"_failure": "test"},
                  approval_url="https://icsacinstitute.org/approve/?t=X", objection_deadline="October 5, 2026")
    ok, _ = na.send_decision(sub_id="ICSAC-SUB-TEST-DOI", source="doi", source_ref="10.5281/zenodo.123456",
                             preprint_doi="10.5281/zenodo.123456", **common)
    out = tmp / "home" / "icsac-submissions" / "test" / "_outbox"
    t = eml_text(out / "ICSAC-SUB-TEST-DOI.eml") if (out / "ICSAC-SUB-TEST-DOI.eml").exists() else ""
    check(ok and "accepted for publication" in t, "DOI route gets the current acceptance email")
    check("Your preprint keeps its own DOI (10.5281/zenodo.123456)" in t, "it names the preprint's DOI")
    check("has no other DOI" not in t, "it does not claim the paper has no other DOI")
    check("approve/?t=X" in t, "it carries the response link")
    ok, _ = na.send_decision(sub_id="ICSAC-SUB-TEST-UP", source="upload", source_ref="paper.pdf", **common)
    t = eml_text(out / "ICSAC-SUB-TEST-UP.eml") if (out / "ICSAC-SUB-TEST-UP.eml").exists() else ""
    check("has no other DOI and is not under review elsewhere" in t and "preprint" not in t.lower(),
          "upload route keeps its no-other-DOI line")
    ok, _ = na.send_published(to="author@example.com", sub_id="ICSAC-SUB-TEST-PUB", title="A test paper",
                              author_name="Test Author", deposit_doi="10.67697/icsac.2026.099",
                              deposit_url="https://icsacinstitute.org/publications/a-test-paper",
                              publications_url="https://icsacinstitute.org/publications/a-test-paper",
                              preprint_doi="10.5281/zenodo.123456", stay_involved=False, tier=2)
    pub = out / "ICSAC-SUB-TEST-PUB-published.eml"
    t = eml_text(pub) if pub.exists() else ""
    check(pub.exists(), "the published email is written (T2 outbox)")
    check("**Preprint:**" in t or "Preprint:" in t, "the published email links the preprint")
    check("Archived copy" not in t, "and does not call the landing page an archived copy")
    pathlib.Path.home = real_home

    print("4. the accept path")
    src = (ROOT / "intake" / "apply_decision.py").read_text()
    check('source in ("upload", "doi")' in src, "a DOI-route accept stages the Crossref deposit")
    check('registrar != "crossref":' in src, "it no longer publishes at accept under Crossref")
    check('(source == "upload" or crossref_path)' in src, "it opens the author's response window")
    check("not crossref_path and worker.stub_pdf_if_doi" in src, "it keeps the PDF the site will host")
    check('crossref_path and source == "upload" and cr_doi' in src, "it stages no second Zenodo copy")
finally:
    pathlib.Path.home = real_home
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALL GREEN' if not failures else f'{len(failures)} FAILURE(S)'}")
sys.exit(1 if failures else 0)
