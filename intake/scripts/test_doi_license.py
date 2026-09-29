#!/usr/bin/env python3
"""Checks for the article licence on the DOI route (2026-09-29): the author
chooses the published article's licence; the preprint keeps its own, recorded
beside it. Offline: the arXiv and Zenodo fetches are replaced by canned
records, no mail, no pings; every write lands in one temporary directory that
is removed at the end, pass or fail. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_doi_license.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TEST_ORCID = "0000-0002-1825-0097"

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="doilicence-"))
try:
    print("1. /api/submit (T2, signed like the proxy)")
    import httpx
    from fastapi.testclient import TestClient
    from intake import intake_server as iss
    import preprint_check
    # The DOI route checks its preprint at submit (2026-09-29); offline, a canned
    # DataCite preprint record stands in for the registries.
    preprint_check._get = lambda url, timeout: (
        [{"DOI": "10.5281/zenodo.1", "RA": "DataCite"}] if "/ra/" in url else
        {"data": {"attributes": {"types": {"resourceTypeGeneral": "Preprint"}, "publisher": "Zenodo",
                                 "titles": [{"title": "T"}], "relatedIdentifiers": []}}})
    iss.HMAC_SECRET = b"licence-test-secret"
    iss.TEST_ORCID_WHITELIST = frozenset({TEST_ORCID})
    subs = tmp / "subs"
    iss.SUBMISSIONS_ROOT = subs
    iss.QUEUE_DIR = subs / "queue"
    iss.COUNTER_FILE = subs / ".counter"
    iss.TEST_SUBMISSIONS_ROOT = subs / "test"
    iss.TEST_QUEUE_DIR = subs / "test" / "queue"
    iss._audit_append = lambda entry, test_mode=False: None
    _n = iter(range(1792000000, 1792000100))
    iss._allocate_test_sub_id = lambda: f"ICSAC-SUB-TEST-{next(_n)}"
    client = TestClient(iss.app)

    def post(data: dict) -> tuple[int, dict]:
        req = httpx.Request("POST", "http://intake/api/submit", data=data)
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

    def record(resp: dict) -> dict:
        sid = resp.get("sub_id") or ""
        return json.loads((subs / "test" / sid / "submission.json").read_text()) if sid else {}

    def dirs() -> int:
        t = subs / "test"
        return len([p for p in t.iterdir() if p.name.startswith("ICSAC-SUB-TEST-")]) if t.exists() else 0

    doi = {"mode": "doi", "name": "Test Author", "email": "author@example.com", "orcid": TEST_ORCID,
           "coi": "true", "exclusivity_acknowledged": "true", "code_data_available": "no",
           "doi": "10.5281/zenodo.1"}
    st, resp = post(dict(doi, license="cc-by-sa-4.0"))
    rec = record(resp)
    check(st in (200, 202) and rec.get("license") == "cc-by-sa-4.0" and rec.get("article_license") == "cc-by-sa-4.0",
          f"DOI route + CC BY-SA: the choice is stored ({st})")
    st, resp = post(dict(doi, license="CC-BY-4.0 "))
    check(st in (200, 202) and record(resp).get("article_license") == "cc-by-4.0", "case and spaces are normalised")
    before = dirs()
    st, resp = post(dict(doi, license="cc-by-nc-4.0"))
    check(st == 400 and "license must be one of" in json.dumps(resp), f"a licence ICSAC does not offer is refused ({st})")
    check(dirs() == before, "a refused licence leaves no submission behind")
    st, resp = post(dict(doi))
    rec = record(resp)
    check(st in (200, 202) and rec.get("license") == "" and rec.get("article_license") is None,
          "no licence sent (an older page): the record is as before")

    print("2. the worker's DOI resolution")
    from intake import submission_worker as worker
    ing = worker.ingest
    real = {k: getattr(ing, k) for k in ("is_arxiv_ref", "arxiv_ref_to_id", "fetch_arxiv_metadata",
                                         "download_arxiv_pdf", "doi_to_record_id", "fetch_metadata",
                                         "download_pdf", "extract_review_data")}

    def fake_pdf(dest_dir: str, name: str) -> str:
        p = Path(dest_dir) / name
        p.write_bytes(b"%PDF-1.4 test\n")
        return str(p)

    ing.fetch_arxiv_metadata = lambda i: {"title": "A", "description": "d", "keywords": [], "license": {"id": ""},
                                          "creators": [{"name": "X"}], "publication_date": "2026-01-01"}
    ing.download_arxiv_pdf = lambda i, dest_dir=None: fake_pdf(dest_dir, f"{i}.pdf")
    ing.fetch_metadata = lambda rid: {"rid": rid}
    ing.download_pdf = lambda m, dest_dir=None: fake_pdf(dest_dir, "z.pdf")
    ing.extract_review_data = lambda m: {"title": "Z", "description": "d", "keywords": [],
                                         "license": {"id": "cc-by-nc-4.0"}, "creators": [{"name": "Y"}]}
    try:
        cases = (
            ("arXiv, author chose CC BY", {"doi": "2401.12345", "article_license": "cc-by-4.0"}, "cc-by-4.0", ""),
            ("Zenodo CC BY-NC, author chose CC BY-SA", {"doi": "10.5281/zenodo.1", "article_license": "cc-by-sa-4.0"},
             "cc-by-sa-4.0", "cc-by-nc-4.0"),
            ("Zenodo, no choice (older record)", {"doi": "10.5281/zenodo.1"}, "cc-by-nc-4.0", "cc-by-nc-4.0"),
        )
        for k, (name, fields, want_lic, want_pre) in enumerate(cases):
            d = tmp / f"w{k}"
            d.mkdir()
            s = dict({"sub_id": f"ICSAC-SUB-TEST-{k}", "source": "doi", "pending_pdf_fetch": True,
                      "form": {"name": "T"}, "license": fields.get("article_license", "")}, **fields)
            (d / "submission.json").write_text(json.dumps(s))
            ok, why = worker._resolve_pending_doi(s["sub_id"], d, s)
            on_disk = json.loads((d / "submission.json").read_text())
            check(ok and on_disk["license"] == want_lic and on_disk["preprint_license"] == want_pre
                  and (d / "paper.pdf").exists(),
                  f"{name}: licence {on_disk.get('license')!r}, preprint's {on_disk.get('preprint_license')!r}")
    finally:
        for k, v in real.items():
            setattr(ing, k, v)

    print("3. the deposit and the curator summary")
    import crossref_deposit as cd
    sub = {"sub_id": "ICSAC-SUB-00099", "title": "T", "abstract": "A.", "source": "doi", "doi": "2401.12345",
           "license": "cc-by-sa-4.0", "article_license": "cc-by-sa-4.0", "preprint_license": "",
           "creators": [{"name": "Test, Author"}]}
    xml = cd.build_deposit_xml(sub, doi="10.67697/icsac.2026.099", landing_url="https://icsacinstitute.org/publications/t",
                               pdf_url=None, content_type="journal-article")
    cd.validate_xml(xml)
    check(b"creativecommons.org/licenses/by-sa/4.0" in xml and b"hasPreprint" in xml,
          "the deposit carries the chosen licence and the preprint link, and validates")
    check(cd.LICENSE_LABELS.get("cc-by-sa-4.0"), "the acceptance email has a label for the chosen licence")
    src = (ROOT / "intake" / "apply_decision.py").read_text()
    check("(the author's choice); the preprint's" in src, "the curator summary shows both licences")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALL GREEN' if not failures else f'{len(failures)} FAILURE(S)'}")
sys.exit(1 if failures else 0)
