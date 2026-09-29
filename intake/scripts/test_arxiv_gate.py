#!/usr/bin/env python3
"""Checks for arXiv access within arXiv's limits (2026-09-29): one connection,
one request every three seconds, a cool-off after a refusal; arXiv metadata
from DataCite first; the DOI route's already-published check. Offline: every
network call is replaced by a canned answer; every write lands in one
temporary directory that is removed at the end, pass or fail. Run from the
repo root with the venv:
    .venv/bin/python intake/scripts/test_arxiv_gate.py
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TEST_ORCID = "0000-0002-1825-0097"

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


tmp = Path(tempfile.mkdtemp(prefix="arxivgate-"))
os.environ["ARXIV_GATE_DIR"] = str(tmp / "gate")
try:
    import arxiv_gate as g
    check(g.MIN_INTERVAL >= 3.0, f"requests are spaced at least three seconds apart ({g.MIN_INTERVAL} s)")

    print("1. the door")
    calls: list[tuple[float, float]] = []
    plan: list = []

    def fake_urlopen(req, timeout=None):
        t0 = time.time()
        action = plan.pop(0) if plan else "ok"
        time.sleep(0.2)
        calls.append((t0, time.time()))
        if action == "ok":
            return Resp(b"<feed/>")
        if isinstance(action, tuple) and action[0] == 429:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {"Retry-After": str(action[1])}, None)
        raise urllib.error.URLError(TimeoutError("timed out"))

    g.urllib.request.urlopen = fake_urlopen
    g.get("https://export.arxiv.org/api/query?id_list=1")
    g.get("https://export.arxiv.org/api/query?id_list=2")
    check(calls[1][0] - calls[0][0] >= g.MIN_INTERVAL - 0.05, "two calls in a row start >= 3.1 s apart")

    real_interval = g.MIN_INTERVAL
    g.MIN_INTERVAL = 0.5          # the thread check needs the order, not the wall clock
    calls.clear()
    threads = [threading.Thread(target=g.get, args=(f"https://export.arxiv.org/api/query?id_list={i}",)) for i in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    spans = sorted(calls)
    check(len(spans) == 5 and all(spans[i + 1][0] >= spans[i][1] for i in range(4)),
          "five threads at once: one connection at a time, never overlapping")
    check(all(spans[i + 1][0] - spans[i][0] >= 0.45 for i in range(4)), "and spaced by the interval")
    g.MIN_INTERVAL = real_interval

    calls.clear()
    plan[:] = [(429, 1), "ok"]
    t0 = time.time()
    body = g.get("https://export.arxiv.org/api/query?id_list=3", attempts=2)
    check(body == b"<feed/>" and len(calls) == 2 and calls[1][0] - calls[0][1] >= 1.0,
          "a 429 is retried after arXiv's Retry-After")

    calls.clear()
    plan[:] = ["timeout"]
    try:
        g.get("https://export.arxiv.org/api/query?id_list=4", attempts=1)
        check(False, "a stall is raised")
    except Exception:
        check(True, "a stall is raised")
    check(g.status().get("closed_until", 0) > time.time() + 500, "and the door shuts for ten minutes")
    n = len(calls)
    t0 = time.time()
    try:
        g.get("https://export.arxiv.org/api/query?id_list=5")
        check(False, "while shut, calls are refused")
    except g.Closed:
        check(len(calls) == n and time.time() - t0 < 0.5, "while shut, calls are refused at once, arXiv is not asked")
    st = g.status()
    st.pop("closed_until")
    (tmp / "gate" / "arxiv-api.json").write_text(json.dumps(st))

    print("2. arXiv metadata from DataCite")
    import submission_intake as si
    DC = {"data": {"attributes": {
        "titles": [{"title": "Distributionally  Robust\n Receive Combining"}],
        "creators": [{"name": "Wang, Shixiong", "givenName": "Shixiong", "familyName": "Wang"},
                     {"name": "Li, Geoffrey Ye", "givenName": "Geoffrey Ye", "familyName": "Li"}],
        "descriptions": [{"descriptionType": "Abstract", "description": "This article investigates signal estimation."}],
        "subjects": [{"subject": "Signal Processing (eess.SP)"}],
        "dates": [{"dateType": "Submitted", "date": "2024-01-22T20:20:48Z"}],
        "rightsList": [{"rights": "arXiv.org perpetual, non-exclusive license",
                        "rightsUri": "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"}],
        "relatedIdentifiers": [{"relationType": "IsVersionOf", "relatedIdentifier": "10.1109/tsp.2025.3582082",
                                "relatedIdentifierType": "DOI"}],
        "publicationYear": 2024}}}
    dc_answer = {"mode": "ok"}
    real_si_urlopen = si.urllib.request.urlopen

    def fake_si_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        assert "api.datacite.org" in url, url
        if dc_answer["mode"] == "404":
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return Resp(json.dumps(DC).encode())

    si.urllib.request.urlopen = fake_si_urlopen
    calls.clear()
    m = si.fetch_arxiv_metadata("2401.12345")
    check(m["title"] == "Distributionally Robust Receive Combining" and m["creators"] == ["Shixiong Wang", "Geoffrey Ye Li"],
          "title and authors, in the shape the pipeline reads")
    check(m["description"].startswith("This article") and m["keywords"] == ["eess.SP"] and m["publication_date"] == "2024-01-22",
          "abstract, category and date")
    check(m["license"] == {"id": "arxiv-nonexclusive-1.0"}, "the licence arXiv's API never gave")
    check(len(calls) == 0, "arXiv's API was not touched")
    DC["data"]["attributes"]["rightsList"] = [{"rightsUri": "https://creativecommons.org/licenses/by/4.0/legalcode"}]
    check(si.fetch_arxiv_metadata("2401.12345")["license"] == {"id": "cc-by-4.0"}, "a /legalcode CC link maps (arXiv writes them so)")
    DC["data"]["attributes"]["rightsList"] = [{"rightsUri": "https://example.org/x", "rightsIdentifier": "CC-BY-SA-4.0"}]
    check(si.fetch_arxiv_metadata("2401.12345")["license"] == {"id": "cc-by-sa-4.0"}, "the SPDX identifier is used when present")
    DC["data"]["attributes"]["rightsList"] = [{"rightsUri": "https://example.org/some-licence"}]
    check(si.fetch_arxiv_metadata("2401.12345")["license"] == {"id": ""}, "an unknown licence maps to nothing")
    DC["data"]["attributes"]["rightsList"] = [{"rightsUri": "https://creativecommons.org/licenses/by/4.0/"}]
    check(si.fetch_arxiv_metadata("2401.12345")["license"] == {"id": "cc-by-4.0"}, "CC BY maps to cc-by-4.0")
    dc_answer["mode"] = "404"
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>'
            '<id>http://arxiv.org/abs/2401.99999v1</id><title>Fallback Paper</title><summary>S</summary>'
            '<published>2024-01-01T00:00:00Z</published><author><name>A Person</name></author>'
            '<arxiv:primary_category term="cs.LG"/></entry></feed>')
    # urllib.request is one module for everyone: one stub answers both hosts.
    def dispatch(req, timeout=None):
        url = req.full_url
        if "api.datacite.org" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        calls.append((time.time(), time.time()))
        return Resp(atom.encode())
    g.urllib.request.urlopen = dispatch
    calls.clear()
    m = si.fetch_arxiv_metadata("2401.99999")
    check(m["title"] == "Fallback Paper" and len(calls) == 1, "no DataCite record: arXiv's API, through the door")
    si.urllib.request.urlopen = real_si_urlopen

    print("3. the citation title search")
    import citation_verify as cv
    seen: list[str] = []
    real_get = g.get
    g.get = lambda url, **k: (seen.append(url), (_ for _ in ()).throw(g.Closed("shut")))[1]
    r = cv._search_arxiv(["Distributionally Robust Receive Combining", "Wang"])
    check(r is None and seen and "export.arxiv.org/api/query" in seen[0], "goes through the door, and a shut door is a quiet miss")
    g.get = real_get

    print("4. the DOI route refuses an already-published preprint")
    import preprint_check as pc
    CR_TARGET = {"10.1109/tsp.2025.3582082": {"type": "journal-article", "container-title": ["IEEE Transactions on Signal Processing"]},
                 "10.9999/preprint.2": {"type": "posted-content", "subtype": "preprint"}}
    DCS = {"10.48550/arXiv.2401.12345": {"types": {"resourceTypeGeneral": "Preprint"}, "publisher": "arXiv",
                                         "titles": [{"title": "T"}],
                                         "relatedIdentifiers": [{"relationType": "IsVersionOf", "relatedIdentifierType": "DOI",
                                                                 "relatedIdentifier": "10.1109/tsp.2025.3582082"}]},
           "10.48550/arXiv.2609.30264": {"types": {"resourceTypeGeneral": "Preprint"}, "publisher": "arXiv",
                                         "titles": [{"title": "T"}], "relatedIdentifiers": []},
           "10.5281/zenodo.7": {"types": {"resourceTypeGeneral": "Preprint"}, "publisher": "Zenodo", "titles": [{"title": "T"}],
                                "relatedIdentifiers": [{"relationType": "IsVersionOf", "relatedIdentifierType": "DOI",
                                                        "relatedIdentifier": "10.5281/zenodo.6"}]},
           "10.48550/arXiv.2501.00001": {"types": {"resourceTypeGeneral": "Preprint"}, "publisher": "arXiv", "titles": [{"title": "T"}],
                                         "relatedIdentifiers": [{"relationType": "IsVersionOf", "relatedIdentifierType": "DOI",
                                                                 "relatedIdentifier": "10.9999/preprint.2"}]}}

    def fake_pc_get(url, timeout):
        import urllib.parse as up
        if "/ra/" in url:
            d = up.unquote(url.split("/ra/", 1)[1])
            return [{"DOI": d, "RA": "DataCite" if d in DCS else "Crossref"}]
        if "api.datacite.org" in url:
            return {"data": {"attributes": DCS[up.unquote(url.split("/dois/", 1)[1])]}}
        return {"message": CR_TARGET[up.unquote(url.split("/works/", 1)[1])]}

    pc._get = fake_pc_get
    try:
        pc.check("10.48550/arXiv.2401.12345")
        check(False, "arXiv preprint published in a journal: refused")
    except pc.Refused as e:
        check("IEEE Transactions on Signal Processing" in str(e), "arXiv preprint published in a journal: refused, journal named")
    check(pc.check("10.48550/arXiv.2609.30264")["verified"], "arXiv preprint with no published version: accepted")
    check(pc.check("10.5281/zenodo.7")["verified"], "Zenodo version pointing at its concept DOI: accepted")
    check(pc.check("10.48550/arXiv.2501.00001")["verified"], "linked to another preprint, not a journal: accepted")

    from fastapi.testclient import TestClient
    import httpx
    from intake import intake_server as iss
    iss.HMAC_SECRET = b"arxivgate-test-secret"
    iss.TEST_ORCID_WHITELIST = frozenset({TEST_ORCID})
    subs = tmp / "subs"
    iss.SUBMISSIONS_ROOT = subs
    iss.QUEUE_DIR = subs / "queue"
    iss.COUNTER_FILE = subs / ".counter"
    iss.TEST_SUBMISSIONS_ROOT = subs / "test"
    iss.TEST_QUEUE_DIR = subs / "test" / "queue"
    iss._audit_append = lambda entry, test_mode=False: None
    _n = iter(range(1793000000, 1793000100))
    iss._allocate_test_sub_id = lambda: f"ICSAC-SUB-TEST-{next(_n)}"
    client = TestClient(iss.app)

    def post(data):
        req = httpx.Request("POST", "http://intake/api/submit", data=data)
        body = req.read()
        ts = str(int(time.time()))
        # Signature v2 (2026-09-29): ts, ORCID, name (none sent), tier, then the body.
        sig = hmac.new(iss.HMAC_SECRET, f"icsac-v2\n{ts}\n{TEST_ORCID}\n\nt2\n".encode() + body,
                       hashlib.sha256).hexdigest()
        r = client.post("/api/submit", content=body, headers={
            "content-type": req.headers["content-type"], "x-icsac-signature": f"v2={sig}",
            "x-icsac-timestamp": ts, "x-icsac-auth-orcid": TEST_ORCID, "x-icsac-test-tier": "t2"})
        return r.status_code, r.json()

    base = {"mode": "doi", "name": "T A", "email": "a@example.com", "orcid": TEST_ORCID, "coi": "true",
            "exclusivity_acknowledged": "true", "code_data_available": "no", "license": "cc-by-4.0"}
    before = len(list((subs / "test").glob("ICSAC-SUB-TEST-*"))) if (subs / "test").exists() else 0
    st, resp = post(dict(base, doi="2401.12345v2"))
    after = len(list((subs / "test").glob("ICSAC-SUB-TEST-*"))) if (subs / "test").exists() else 0
    check(st == 422 and "already published" in resp.get("message", "") and before == after,
          f"intake: a published arXiv paper on the DOI route is refused, nothing written ({st})")
    st, resp = post(dict(base, doi="10.48550/arXiv.2609.30264"))
    rec = json.loads((subs / "test" / resp["sub_id"] / "submission.json").read_text()) if st in (200, 202) else {}
    check(st in (200, 202) and (rec.get("preprint_meta") or {}).get("verified"),
          f"intake: an unpublished arXiv paper is accepted and its check is recorded ({st})")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALL GREEN' if not failures else f'{len(failures)} FAILURE(S)'}")
sys.exit(1 if failures else 0)
