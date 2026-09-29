#!/usr/bin/env python3
"""Checks for the code and data link policy (2026-09-28). Offline: no network,
no mail, no pings; every write lands in one temporary directory that is
removed at the end, pass or fail. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_code_data_link.py

Covers: the claim check on real papers and on made-up sentences; the form
answer through the real /api/submit handler (signed like the proxy signs it);
the worker holding a paper before the panel and the curator override; the
revise email the hold produces; the accept guard; the Zenodo relation; the
registry field.
"""
from __future__ import annotations

import glob
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

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


PAPER_PDF = Path.home() / "icsac-submissions" / "ICSAC-SUB-00008" / "paper.pdf"
PAPERS = Path.home() / "Desktop" / "icsac" / "icsacinstitute.org" / "public" / "papers"
TEST_ORCID = "0000-0002-1825-0097"

tmp = Path(tempfile.mkdtemp(prefix="codelink-"))
real_home = pathlib.Path.home
try:
    import code_availability as ca
    import submission_intake as ingest

    # ── 1. the claim check ───────────────────────────────────────────────────
    print("1. claim check")
    vim_text = ingest.extract_pdf_text(str(PAPER_PDF))
    r = ca.assess({}, vim_text)
    check(r["missing_link"], "[author]: claims code/data, no link anywhere -> held")
    check(any("accompanying archive" in c for c in r["claims"]), "[author]: the archive sentence is quoted")
    link = "https://github.com/example/generator"
    check(not ca.assess({"code_data": {"available": True, "url": link}}, vim_text)["missing_link"],
          "[author] + form link -> not held")
    check(not ca.assess({"related_identifiers": [{"identifier": link, "relation": "isSupplementedBy"}]},
                        vim_text)["missing_link"], "[author] + 'is supplemented by' identifier -> not held")
    check(not ca.assess({"related_identifiers": [{"identifier": link, "relation": "isSupplementTo"}]},
                        vim_text)["missing_link"], "[author] + older 'is supplement to' identifier -> not held")
    check(ca.assess({"related_identifiers": [{"identifier": link, "relation": "cites"}]},
                    vim_text)["missing_link"], "[author] + a 'cites' identifier -> still held")

    na = "Methods.\nWe prove a theorem.\n\nData availability: Not applicable.\n\nReferences\nSmith, J. (2020)."
    check(ca.find_claims(ca.body_text(na)) == [], "'Data availability: Not applicable.' is no claim")
    none = "Data availability statement\nNo new data were generated or analysed in this study."
    check(ca.find_claims(none) == [], "'No new data were generated' is no claim")
    plain = "The data show a clear trend, and the code of conduct was followed. Figure 2 plots the data."
    check(ca.find_claims(plain) == [], "ordinary mentions of data and code are no claim")
    req = "Results.\nData are available on request from the author.\n"
    check(ca.assess({}, req)["missing_link"], "'available on request' with no link -> held")
    body_link = "Code availability. The scripts are available at https://github.com/a/b for anyone."
    check(not ca.assess({}, body_link)["missing_link"], "claim with a repository link in the body -> not held")
    refs_only = ("Code availability. The code is available from the author.\n\nReferences\n"
                 "Doe, A. (2021). Tool. https://github.com/doe/tool\n")
    check(ca.assess({}, refs_only)["missing_link"], "a link only in the reference list does not count")
    gap = "Complete source code and validated data files are available in the GitHub Repository."
    check(len(ca.find_claims(gap)) == 1, "words between 'code' and 'are available' still read as a claim")
    for p in sorted(glob.glob(str(PAPERS / "*.pdf"))):
        if p.endswith("-icsac.pdf") or not Path(p).stem.isdigit():
            continue   # founding papers are the Zenodo-record PDFs; intake papers below
        rr = ca.assess({}, ingest.extract_pdf_text(p))
        check(not rr["missing_link"], f"founding paper {Path(p).stem}: not held "
              f"({len(rr['claims'])} claim(s), {len(rr['links']['in_paper'])} link(s))")
    # The first intake paper (2026-09-29): its Declarations promise an archive with no
    # link in the text; the link came from the author's reply and rides on code_data.
    vim = PAPERS / "ICSAC-SUB-00008.pdf"
    if vim.exists():
        vt = ingest.extract_pdf_text(str(vim))
        check(ca.assess({}, vt)["missing_link"], "the published paper on its text alone: held (the claim has no link)")
        check(not ca.assess({"code_data": {"available": True, "url": "https://doi.org/10.5281/zenodo.23042786"}}, vt)["missing_link"],
              "the published paper with its author's archive link: not held")

    # ── 2. the form answer through the real handler ──────────────────────────
    print("2. /api/submit (T2, signed like the proxy)")
    import httpx
    from fastapi.testclient import TestClient
    from intake import intake_server as iss
    iss.HMAC_SECRET = b"codelink-test-secret"
    iss.TEST_ORCID_WHITELIST = frozenset({TEST_ORCID})
    subs = tmp / "subs"
    iss.SUBMISSIONS_ROOT = subs
    iss.QUEUE_DIR = subs / "queue"
    iss.COUNTER_FILE = subs / ".counter"
    iss.TEST_SUBMISSIONS_ROOT = subs / "test"
    iss.TEST_QUEUE_DIR = subs / "test" / "queue"
    audit_iss: list[dict] = []
    iss._audit_append = lambda entry, test_mode=False: audit_iss.append(entry)
    _n = iter(range(1790000000, 1790000100))
    iss._allocate_test_sub_id = lambda: f"ICSAC-SUB-TEST-{next(_n)}"
    client = TestClient(iss.app)
    pdf_bytes = PAPER_PDF.read_bytes()

    def submit(**extra) -> tuple[int, dict]:
        data = {
            "mode": "upload", "name": "Test Author", "email": "author@example.com",
            "orcid": TEST_ORCID, "coi": "true", "exclusivity_acknowledged": "true",
            "deposit_consent": "true", "title": "A test paper on boundaries",
            "abstract": "An abstract long enough to pass the fifty-character minimum for uploads.",
            "keywords": "testing, boundaries", "license": "cc-by-4.0", "resource_type": "preprint",
            "publication_date": "2026-09-28", "subject": "", "funding": "",
            "creators": json.dumps([{"name": "Test Author", "orcid": TEST_ORCID}]),
            "related_identifiers": "[]", **extra,
        }
        req = httpx.Request("POST", "http://intake/api/submit", data=data,
                            files={"pdf": ("paper.pdf", pdf_bytes, "application/pdf")})
        body = req.read()
        ts = str(int(time.time()))
        # Signature v2 (2026-09-29): ts, ORCID, name (none sent), tier, then the body.
        sig = hmac.new(iss.HMAC_SECRET, f"icsac-v2\n{ts}\n{TEST_ORCID}\n\nt2\n".encode() + body,
                       hashlib.sha256).hexdigest()
        resp = client.post("/api/submit", content=body, headers={
            "content-type": req.headers["content-type"],
            "x-icsac-signature": f"v2={sig}", "x-icsac-timestamp": ts,
            "x-icsac-auth-orcid": TEST_ORCID, "x-icsac-test-tier": "t2",
        })
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"raw": resp.text[:300]}

    def record(resp: dict) -> dict:
        sid = resp.get("sub_id") or resp.get("id") or ""
        return json.loads((subs / "test" / sid / "submission.json").read_text()) if sid else {}

    st, resp = submit(code_data_available="yes", code_data_url="https://github.com/example/generator")
    check(st in (200, 202) and record(resp).get("code_data") == {"available": True, "url": "https://github.com/example/generator"},
          f"yes + link -> stored ({st})")
    linked_id = resp.get("sub_id", "")
    st, resp = submit(code_data_available="yes", code_data_url="")
    check(st == 400 and "link" in json.dumps(resp), f"yes without a link -> 400 ({st})")
    for bad in ("ftp://example.org/x", "javascript:alert(1)", "https://github.com/a b",
                "http://localhost/x", "https://user@example.org/x", "https://example.org/" + "a" * 490):
        st, resp = submit(code_data_available="yes", code_data_url=bad)
        check(st == 400, f"bad link refused: {bad[:40]} ({st})")
    st, resp = submit(code_data_available="maybe")
    check(st == 400, f"an answer other than yes/no -> 400 ({st})")
    st, resp = submit(code_data_available="no", code_data_url="https://github.com/example/x")
    check(st in (200, 202) and record(resp).get("code_data") == {"available": False, "url": None},
          f"no -> stored as no, stray link dropped ({st})")
    st, resp = submit()
    held_id = resp.get("sub_id", "")
    check(st in (200, 202) and record(resp).get("code_data") == {"available": None, "url": None},
          f"no answer (older form) -> stored as unknown ({st})")
    st, resp = submit()
    held2_id = resp.get("sub_id", "")
    check("isSupplementedBy" in iss.RELATION_TYPES, "'is supplemented by' is an accepted relation")
    st, resp = submit(related_identifiers=json.dumps([{"identifier": "https://github.com/a/b",
                                                       "relation": "isSupplementedBy"}]))
    check(st in (200, 202), f"a related identifier 'is supplemented by' is accepted ({st})")
    check(all(e.get("event") != "code_data_link_missing" for e in audit_iss), "intake itself holds nothing")

    # ── 3. the worker: hold before the panel, override, linked paper ─────────
    print("3. worker")
    from intake import submission_worker as worker
    worker.SUBMISSIONS_ROOT = subs
    worker.QUEUE_DIR = subs / "queue"
    worker.TEST_SUBMISSIONS_ROOT = subs / "test"
    worker.TEST_QUEUE_DIR = subs / "test" / "queue"
    worker.TEST_REVIEWS_DIR = tmp / "reviews-test"
    audit_w: list[dict] = []
    worker._audit = lambda entry, test_mode=False: audit_w.append(entry)
    alerts: list = []
    worker._alert_remote = lambda *a, **k: alerts.append(a)
    pings: list = []
    worker.notify.send_telegram = lambda *a, **k: pings.append(a)
    worker.notify.send_to_curator = lambda *a, **k: pings.append(a)
    panel_calls: list[str] = []

    def fake_panel(review_data):
        panel_calls.append(review_data.get("title", ""))
        return "", {"recommendation": "PAUSED_AI_FAILURE"}
    worker.review.review_paper = fake_panel

    def state(sid: str) -> dict:
        return json.loads((subs / "test" / sid / "state.json").read_text())

    worker._process_locked(held_id)
    s = state(held_id)
    check(s.get("state") == "awaiting_decision" and s.get("pending_recommendation") == "REVISE_AND_RESUBMIT"
          and s.get("precheck") == "code_data_link_missing", "no link: held for a revise before the panel")
    check(panel_calls == [], "no link: the panel did not run")
    cda = json.loads((subs / "test" / held_id / "code_availability.json").read_text())
    check(cda["missing_link"] and cda["claims"], "no link: code_availability.json records the claims")
    check(any(e.get("event") == "code_data_link_missing" for e in audit_w), "no link: audited")
    check(pings == [] and alerts == [], "T2: no curator ping, no alert")

    worker._process_locked(linked_id)
    check(panel_calls == ["A test paper on boundaries"], "form link: the panel runs")
    check(state(linked_id).get("state") == "paused_panel_failure", "form link: normal path continues")

    from intake import code_check_override as cco
    rc = cco.main(["code-check-override.sh", held_id])
    s = state(held_id)
    check(rc == 0 and s.get("code_check_override") and s.get("precheck") == "overridden",
          "override: recorded, precheck cleared")
    check((subs / "test" / "queue" / held_id).exists(), "override: queued again")
    check(cco.main(["code-check-override.sh", held_id]) == 3, "override twice: refused")
    worker._process_locked(held_id)
    check(len(panel_calls) == 2, "override: the panel runs on the next pass")

    real_assess = worker.code_availability.assess
    def broken(*a, **k):
        raise RuntimeError("simulated check failure")
    worker.code_availability.assess = broken
    st_, resp_ = submit()
    broken_id = resp_.get("sub_id", "")
    worker._process_locked(broken_id)
    worker.code_availability.assess = real_assess
    check(len(panel_calls) == 3, "a broken check fails open: the panel still runs")
    check(any(e.get("event") == "code_check_failed" for e in audit_w), "a broken check is audited")
    check(pings == [] and alerts == [], "T2: a broken check pings no one (test tier)")

    # ── 4. decisions on a held paper: the revise email, the accept guard ─────
    print("4. decisions")
    worker._process_locked(held2_id)
    check(state(held2_id).get("precheck") == "code_data_link_missing", "second paper held")
    from intake import apply_decision as ad
    ad.SUBMISSIONS_ROOT = subs
    ad.TEST_SUBMISSIONS_ROOT = subs / "test"
    ad._audit = lambda entry, test_mode=False: audit_w.append(entry)
    ad.notify.send_to_curator = lambda *a, **k: pings.append(a)
    pathlib.Path.home = classmethod(lambda cls: tmp / "home")
    rc = ad.main(["apply_decision.py", held2_id, "accept"])
    check(rc == 3 and not state(held2_id).get("decision"), "accept on a held paper: refused, nothing recorded")
    rc = ad.main(["apply_decision.py", held2_id, "revise"])
    pathlib.Path.home = real_home
    check(rc in (0, None) and state(held2_id).get("decision") == "revise", f"revise: recorded (rc={rc})")
    eml = tmp / "home" / "icsac-submissions" / "test" / "_outbox" / f"{held2_id}.eml"
    check(eml.exists(), "revise: the email is written (T2 outbox)")
    if eml.exists():
        import email as _email
        import email.policy as _policy
        msg = _email.message_from_bytes(eml.read_bytes(), policy=_policy.default)
        text = "".join(p.get_payload(decode=True).decode("utf-8", "replace")
                       for p in msg.walk() if p.get_content_type() == "text/plain")
        norm = " ".join(text.split())
        check("revise and resubmit" in str(msg["Subject"] or "").lower(), f"revise email: subject ({msg['Subject']})")
        check("accompanying archive" in norm, "revise email: quotes the paper's own sentence")
        check("Harvard Dataverse" in norm and "hosted by the author" in norm, "revise email: says where and how it shows")
        check("{{" not in text and "}}" not in text, "revise email: no unfilled fields")
        check("hasn't been sent to the reviewer panel" in norm, "revise email: says the panel did not run")
        check(not any(p.get_content_type() == "application/pdf" for p in msg.walk()), "revise email: no report attached")
        check("reply" not in norm.lower(), "revise email: invites no reply")

    # ── 5. Zenodo relation and the registry field ────────────────────────────
    print("5. deposit + registry")
    import repository_deposit as rd
    sub = {"title": "T", "abstract": "A", "keywords": ["k"], "license": "cc-by-4.0",
           "publication_date": "2026-09-28", "resource_type": "preprint",
           "creators": [{"name": "Author, Test"}], "related_identifiers": [],
           "code_data": {"available": True, "url": "https://github.com/example/generator"}}
    md = rd._build_metadata(sub)
    check({"identifier": "https://github.com/example/generator", "relation": "isSupplementedBy"}
          in md.get("related_identifiers", []), "Zenodo: the link is 'is supplemented by'")
    sub["related_identifiers"] = [{"identifier": "https://github.com/example/generator", "relation": "isSupplementedBy"}]
    md = rd._build_metadata(sub)
    check(len(md["related_identifiers"]) == 1, "Zenodo: no duplicate when the author also listed it")
    import publications as pub
    site = tmp / "site"; (site / "src" / "data").mkdir(parents=True)
    (site / "src" / "data" / "accepted.json").write_text("[]")
    pub.WEBSITE_REPO = str(site); pub.REGISTRY_PATH = str(site / "src" / "data" / "accepted.json")
    check(pub.code_data_url(sub) == "https://github.com/example/generator", "registry: helper reads the form link")
    e = pub.upsert_entry({"title": "T", "authors": ["A"], "doi": "10.67697/icsac.2026.999",
                          "source": "submission-pdf", "code_data_url": pub.code_data_url(sub)})
    check(e.get("code_data_url") == "https://github.com/example/generator", "registry: the entry keeps code_data_url")
    check(pub.code_data_url({"code_data": {"available": False, "url": None}}) is None, "registry: 'no' gives no link")
finally:
    pathlib.Path.home = real_home
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(0 if not failures else 1)
