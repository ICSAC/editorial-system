#!/usr/bin/env python3
"""Checks for preprint DOIs from any server (policy 2026-09-28) and the
exclusivity confirmation on both routes. Offline: the DOI registries are
replaced by canned records, no mail, no pings; every write lands in one
temporary directory that is removed at the end, pass or fail. Run from the
repo root with the venv:
    .venv/bin/python intake/scripts/test_preprint_doi.py
"""
from __future__ import annotations

import email as _email
import email.policy as _policy
import hashlib
import hmac
import json
import pathlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PAPER_PDF = Path.home() / "icsac-submissions" / "ICSAC-SUB-00008" / "paper.pdf"
TEST_ORCID = "0000-0002-1825-0097"

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


# Canned registry answers, keyed by URL fragment.
RA = {"10.20944/preprints202607.0102.v1": "Crossref", "10.1101/2024.09.14.613029": "Crossref",
      "10.1038/nature12373": "Crossref", "10.1364/opticaopen.29459153.v1": "Crossref",
      "10.48550/arXiv.2401.12345": "DataCite", "10.5281/zenodo.1": "DataCite",
      "10.51094/jxiv.1": "JaLC"}
CR = {
    "10.20944/preprints202607.0102.v1": {"type": "posted-content", "subtype": "preprint",
                                         "title": ["A test paper on boundaries"], "publisher": "MDPI AG"},
    "10.1101/2024.09.14.613029": {"type": "posted-content", "subtype": "preprint",
                                  "title": ["Something else entirely about proteins"],
                                  "institution": [{"name": "bioRxiv"}]},
    "10.1038/nature12373": {"type": "journal-article", "title": ["Nanometre-scale thermometry"],
                            "container-title": ["Nature"]},
    "10.1364/opticaopen.29459153.v1": {"type": "posted-content", "subtype": "preprint", "title": ["X"],
                                       "relation": {"is-preprint-of": [{"id": "10.1364/OE.572415"}]}},
}
DC = {"10.48550/arXiv.2401.12345": {"types": {"resourceTypeGeneral": "Preprint"}, "publisher": "arXiv",
                                    "titles": [{"title": "A test paper on boundaries"}]},
      "10.5281/zenodo.1": {"types": {"resourceTypeGeneral": "JournalArticle"}, "publisher": {"name": "Zenodo"},
                           "titles": [{"title": "A test paper on boundaries"}]}}
NETWORK_DOWN = {"on": False}


def fake_get(url: str, timeout: float):
    import urllib.parse
    if NETWORK_DOWN["on"]:
        raise OSError("network down")
    tail = urllib.parse.unquote(url.split("/ra/", 1)[-1] if "/ra/" in url else
                                url.split("/works/", 1)[-1] if "/works/" in url else url.split("/dois/", 1)[-1])
    if "/ra/" in url:
        return [{"DOI": tail, "RA": RA[tail]}] if tail in RA else [{"DOI": tail, "status": "DOI does not exist"}]
    if "/works/" in url:
        return {"message": CR[tail]}
    return {"data": {"attributes": DC[tail]}}


tmp = Path(tempfile.mkdtemp(prefix="preprint-"))
real_home = pathlib.Path.home
try:
    import preprint_check as pc
    pc._get = fake_get

    print("1. the check")
    r = pc.check("10.20944/preprints202607.0102.v1", title="A test paper on boundaries")
    check(r["verified"] and r["type"] == "posted-content/preprint" and not r["flags"], "Preprints.org preprint: accepted, clean")
    r = pc.check("10.1101/2024.09.14.613029", title="A test paper on boundaries")
    check(r["server"] == "bioRxiv" and any("title differs" in f for f in r["flags"]), "bioRxiv preprint with another title: accepted, flagged")
    for d, why in (("10.1038/nature12373", "a journal article"), ("10.1364/opticaopen.29459153.v1", "a preprint already published"),
                   ("10.9999/nope", "a DOI that does not exist")):
        try:
            pc.check(d)
            check(False, f"refused: {why}")
        except pc.Refused:
            check(True, f"refused: {why}")
    r = pc.check("10.48550/arXiv.2401.12345")
    check(r["verified"] and r["type"] == "Preprint" and not r["flags"], "arXiv (DataCite Preprint): accepted, clean")
    r = pc.check("10.5281/zenodo.1")
    check(r["server"] == "Zenodo" and any("JournalArticle" in f for f in r["flags"]), "Zenodo filed as a journal article: accepted, flagged")
    r = pc.check("10.51094/jxiv.1")
    check(r["ra"] == "JaLC" and not r["verified"] and r["flags"], "another agency (JaLC): accepted, flagged")
    NETWORK_DOWN["on"] = True
    r = pc.check("10.20944/preprints202607.0102.v1")
    NETWORK_DOWN["on"] = False
    check(not r["verified"] and any("could not be reached" in f for f in r["flags"]), "registries down: fails open, flagged")
    check(pc.normalize("https://doi.org/10.1101/2024.09.14.613029") == "10.1101/2024.09.14.613029", "resolver URL normalised")
    check(pc.normalize("arXiv:2401.12345v3") == "10.48550/arXiv.2401.12345", "arXiv: prefix and version normalised")
    check(pc.normalize("not a doi") is None, "garbage refused")

    print("2. /api/submit (T2, signed like the proxy)")
    import httpx
    from fastapi.testclient import TestClient
    from intake import intake_server as iss
    iss.HMAC_SECRET = b"preprint-test-secret"
    iss.TEST_ORCID_WHITELIST = frozenset({TEST_ORCID})
    subs = tmp / "subs"
    iss.SUBMISSIONS_ROOT = subs
    iss.QUEUE_DIR = subs / "queue"
    iss.COUNTER_FILE = subs / ".counter"
    iss.TEST_SUBMISSIONS_ROOT = subs / "test"
    iss.TEST_QUEUE_DIR = subs / "test" / "queue"
    iss._audit_append = lambda entry, test_mode=False: None
    _n = iter(range(1791000000, 1791000100))
    iss._allocate_test_sub_id = lambda: f"ICSAC-SUB-TEST-{next(_n)}"
    client = TestClient(iss.app)
    pdf_bytes = PAPER_PDF.read_bytes()

    def post(data: dict, with_pdf: bool) -> tuple[int, dict]:
        req = httpx.Request("POST", "http://intake/api/submit", data=data,
                            files={"pdf": ("paper.pdf", pdf_bytes, "application/pdf")} if with_pdf else None)
        body = req.read()
        ts = str(int(time.time()))
        # Signature v2 (2026-09-29): ts, ORCID, name (none sent), tier, then the body.
        sig = hmac.new(iss.HMAC_SECRET, f"icsac-v2\n{ts}\n{TEST_ORCID}\n\nt2\n".encode() + body,
                       hashlib.sha256).hexdigest()
        resp = client.post("/api/submit", content=body, headers={
            "content-type": req.headers["content-type"], "x-icsac-signature": f"v2={sig}",
            "x-icsac-timestamp": ts, "x-icsac-auth-orcid": TEST_ORCID, "x-icsac-test-tier": "t2"})
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"raw": resp.text[:300]}

    base = {"name": "Test Author", "email": "author@example.com", "orcid": TEST_ORCID, "coi": "true",
            "exclusivity_acknowledged": "true", "code_data_available": "no"}
    upload = dict(base, mode="upload", deposit_consent="true", title="A test paper on boundaries",
                  abstract="An abstract long enough to pass the fifty-character minimum for uploads.",
                  keywords="testing", license="cc-by-4.0", resource_type="preprint", publication_date="2026-09-28",
                  subject="", funding="", creators=json.dumps([{"name": "Test Author", "orcid": TEST_ORCID}]),
                  related_identifiers="[]")

    def record(resp: dict) -> dict:
        sid = resp.get("sub_id") or ""
        return json.loads((subs / "test" / sid / "submission.json").read_text()) if sid else {}

    def dirs() -> int:
        return len([p for p in (subs / "test").iterdir() if p.name.startswith("ICSAC-SUB-TEST-")]) if (subs / "test").exists() else 0

    st, resp = post(dict(upload, preprint_doi="https://doi.org/10.20944/preprints202607.0102.v1"), True)
    rec = record(resp)
    check(st in (200, 202) and rec.get("preprint_doi") == "10.20944/preprints202607.0102.v1"
          and (rec.get("preprint_meta") or {}).get("verified"), f"upload + Preprints.org DOI: stored, verified ({st})")
    check(rec.get("form", {}).get("exclusivity_acknowledged") is True, "upload: the exclusivity confirmation is stored")
    up_rec = rec
    st, resp = post(dict(upload), True)
    check(st in (200, 202) and record(resp).get("preprint_doi") is None, "upload without a preprint: none stored")
    before = dirs()
    st, resp = post(dict(upload, preprint_doi="10.1038/nature12373"), True)
    check(st == 422 and resp.get("error") == "preprint_doi_refused" and "Nature" in json.dumps(resp),
          f"upload + a journal article DOI: refused with the journal named ({st})")
    check(dirs() == before, "a refused preprint leaves no submission behind")
    st, resp = post(dict(upload, preprint_doi="banana"), True)
    check(st == 422 and resp.get("error") == "preprint_doi_invalid", f"upload + garbage: refused ({st})")
    st, resp = post(dict(base, mode="doi", doi="10.5281/zenodo.1"), False)
    check(st in (200, 202) and record(resp).get("form", {}).get("exclusivity_acknowledged") is True,
          f"DOI route: the exclusivity confirmation is stored ({st})")
    st, resp = post(dict(base, mode="doi", doi="10.1101/2024.09.14.613029"), False)
    check(st == 422 and "submit/upload?preprint_doi=10.1101/2024.09.14.613029" in json.dumps(resp),
          f"DOI route + a bioRxiv DOI: sent to the upload form with the DOI ({st})")

    print("3. the deposit and the emails")
    import crossref_deposit as cd
    check(cd.preprint_doi(up_rec) == "10.20944/preprints202607.0102.v1", "the pipeline reads the upload route's preprint")
    sub = dict(up_rec, sub_id="ICSAC-SUB-00099")
    xml = cd.build_deposit_xml(sub, doi="10.67697/icsac.2026.099", landing_url="https://icsacinstitute.org/publications/t",
                               pdf_url=None, content_type="journal-article")
    cd.validate_xml(xml)
    check(b'relationship-type="hasPreprint"' in xml and b"10.20944/preprints202607.0102.v1" in xml,
          "upload + preprint: the deposit validates and links the preprint")
    from intake import notify_author as na
    pathlib.Path.home = classmethod(lambda cls: tmp / "home")
    common = dict(to="author@example.com", title="T", author_name="A", verdict="accept", panel_report_md="",
                  rqc_md="", tier=2, compaction_manifest={"_failure": "test"},
                  approval_url="https://icsacinstitute.org/approve/?t=X", objection_deadline="October 5, 2026")
    out = tmp / "home" / "icsac-submissions" / "test" / "_outbox"

    def body(sid: str) -> str:
        p = out / f"{sid}.eml"
        if not p.exists():
            return ""
        m = _email.message_from_bytes(p.read_bytes(), policy=_policy.default)
        return "".join(x.get_payload(decode=True).decode() for x in m.walk() if x.get_content_type() == "text/plain")

    na.send_decision(sub_id="ICSAC-SUB-TEST-A", source="upload", source_ref="paper.pdf",
                     preprint_doi="10.20944/preprints202607.0102.v1", exclusivity_confirmed=True, **common)
    t = body("ICSAC-SUB-TEST-A")
    check("keeps its own DOI (10.20944/preprints202607.0102.v1)" in t and "not under review elsewhere, as confirmed" in t
          and "has no other DOI" not in t, "upload + preprint: the email names the preprint and the confirmation")
    na.send_decision(sub_id="ICSAC-SUB-TEST-B", source="doi", source_ref="10.5281/zenodo.1",
                     preprint_doi="10.5281/zenodo.1", exclusivity_confirmed=False, **common)
    t = body("ICSAC-SUB-TEST-B")
    check("keeps its own DOI" in t and "as confirmed at submission" not in t,
          "a paper whose confirmation was never recorded is not told it confirmed")
    pathlib.Path.home = real_home
    src = (ROOT / "intake" / "apply_decision.py").read_text()
    check("exclusivity_confirmed=form.get(\"exclusivity_acknowledged\") is True" in src, "the accept passes the recorded confirmation")
finally:
    pathlib.Path.home = real_home
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALL GREEN' if not failures else f'{len(failures)} FAILURE(S)'}")
sys.exit(1 if failures else 0)
