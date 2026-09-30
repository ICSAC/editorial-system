#!/usr/bin/env python3
"""Consents at submission (2026-09-30). Offline: no network, no mail, no
pings; every write lands in one temporary directory removed at the end,
pass or fail. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_consents_at_submission.py

Covers: the process acknowledgement (required once the form sends
form_version; older forms unchanged), the version stored with its time,
the exclusions at submission (validated ids, stored, pre-ticked on the
response page, seeded into the state when the window opens, replaced by the
author's response), the newsletter opt-in (stored on the record, appended to
the list only on the production path, never on a test tier), the newsletter
module (fold, unsubscribe, token), and the unsubscribe endpoint.
"""
from __future__ import annotations

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
TEST_ORCID = "0000-0002-1825-0097"

tmp = Path(tempfile.mkdtemp(prefix="consents-"))
try:
    import httpx
    from fastapi.testclient import TestClient
    from intake import intake_server as iss
    from intake import author_approval as aa
    from intake import newsletter as nl

    iss.HMAC_SECRET = b"consents-test-secret"
    iss.TEST_ORCID_WHITELIST = frozenset({TEST_ORCID})
    subs = tmp / "subs"
    iss.SUBMISSIONS_ROOT = subs
    iss.QUEUE_DIR = subs / "queue"
    iss.COUNTER_FILE = subs / ".counter"
    iss.TEST_SUBMISSIONS_ROOT = subs / "test"
    iss.TEST_QUEUE_DIR = subs / "test" / "queue"
    audit: list[dict] = []
    iss._audit_append = lambda entry, test_mode=False: audit.append(entry)
    _n = iter(range(1790100000, 1790100100))
    iss._allocate_test_sub_id = lambda: f"ICSAC-SUB-TEST-{next(_n)}"
    client = TestClient(iss.app)
    pdf_bytes = PAPER_PDF.read_bytes()

    def submit(**extra) -> tuple[int, dict]:
        data = {
            "mode": "upload", "name": "Test Author", "email": "author@example.com",
            "orcid": TEST_ORCID, "coi": "true", "exclusivity_acknowledged": "true",
            "deposit_consent": "true", "title": "A test paper on consents",
            "abstract": "An abstract long enough to pass the fifty-character minimum for uploads.",
            "keywords": "testing, consents", "license": "cc-by-4.0", "resource_type": "preprint",
            "publication_date": "2026-09-30", "subject": "", "funding": "",
            "creators": json.dumps([{"name": "Test Author", "orcid": TEST_ORCID}]),
            "related_identifiers": "[]", "code_data_available": "no", **extra,
        }
        data = {k: v for k, v in data.items() if v is not None}
        req = httpx.Request("POST", "http://intake/api/submit", data=data,
                            files={"pdf": ("paper.pdf", pdf_bytes, "application/pdf")})
        body = req.read()
        ts = str(int(time.time()))
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
        sid = resp.get("sub_id") or ""
        return json.loads((subs / "test" / sid / "submission.json").read_text()) if sid else {}

    NEW = {"form_version": "2026-09-30", "process_ack": "true", "process_ack_version": "2026-09-30"}

    print("1. the acknowledgement")
    st, resp = submit()
    f = record(resp).get("form", {})
    check(st in (200, 202) and f.get("process_ack") is None and f.get("form_version") is None,
          f"older form (no form_version): accepted, nothing recorded ({st})")
    st, resp = submit(form_version="2026-09-30", process_ack_version="2026-09-30")
    check(st == 400 and "acknowledgement" in json.dumps(resp), f"new form without the tick -> 400 ({st})")
    st, resp = submit(**NEW)
    f = record(resp).get("form", {})
    check(st in (200, 202) and f.get("process_ack") is True and f.get("process_ack_version") == "2026-09-30"
          and (f.get("process_ack_at") or "").endswith("Z"), f"new form with the tick: version + time stored ({st})")
    check(f.get("exclusions_at_submission") == [] and f.get("newsletter_opt_in") is None,
          "no exclusions, newsletter unanswered -> [] / None")
    st, resp = submit(**dict(NEW, process_ack_version=""))
    check(st == 400, f"tick without a version -> 400 ({st})")

    print("2. exclusions at submission")
    st, resp = submit(**NEW, exclusions="social,newsletter")
    sid = resp.get("sub_id", "")
    f = record(resp).get("form", {})
    check(st in (200, 202) and f.get("exclusions_at_submission") == ["newsletter", "social"],
          f"two exclusions stored, sorted ({st})")
    st, resp = submit(**NEW, exclusions="social,gold_plating")
    check(st == 400 and "unknown exclusion" in json.dumps(resp), f"unknown id -> 400 ({st})")
    st, resp = submit(**NEW, exclusions="")
    check(st in (200, 202) and record(resp)["form"]["exclusions_at_submission"] == [], "empty list ok")

    sub_dir = subs / "test" / sid
    (sub_dir / "state.json").write_text(json.dumps({"state": "completed", "decision": "accept"}))
    check(aa.submission_exclusions(sub_dir) == ["newsletter", "social"], "submission_exclusions reads the record")
    aa.SUBMISSIONS_ROOT, aa.TEST_SUBMISSIONS_ROOT = subs, subs / "test"
    issued = aa.issue(sub_dir)
    st_json = json.loads((sub_dir / "state.json").read_text())
    check(st_json.get("author_exclusions") == ["newsletter", "social"] and st_json.get("promotion_opt_out") is True
          and st_json.get("persistence_opt_out") is False, "issue() seeds the state with the submission-time exclusions")
    rec = aa._load(sub_dir)
    view = aa.public_view(sub_dir, rec)
    check(view["exclusions"] == ["newsletter", "social"], "response page pre-ticks them before any response")
    aa.record(sub_dir, rec, "approve", "", ["persistence"], quote_ok=False, orcid=TEST_ORCID, audit=lambda e: None)
    st_json = json.loads((sub_dir / "state.json").read_text())
    rec = aa._load(sub_dir)
    check(st_json.get("author_exclusions") == ["persistence"] and st_json.get("promotion_opt_out") is False
          and st_json.get("persistence_opt_out") is True, "the author's response replaces the seed")
    check(aa.public_view(sub_dir, rec)["exclusions"] == ["persistence"], "page shows the response's list after it")
    # a paper with no exclusions at submission: issue() seeds nothing
    st, resp = submit(**NEW)
    sd2 = subs / "test" / resp["sub_id"]
    (sd2 / "state.json").write_text(json.dumps({"state": "completed", "decision": "accept"}))
    aa.issue(sd2)
    check("author_exclusions" not in json.loads((sd2 / "state.json").read_text()), "no exclusions -> nothing seeded")

    print("3. newsletter")
    st, resp = submit(**NEW, newsletter_opt_in="true")
    f = record(resp).get("form", {})
    check(st in (200, 202) and f.get("newsletter_opt_in") is True and (f.get("newsletter_opt_in_at") or "").endswith("Z")
          and f.get("newsletter_wording_version") == "2026-09-30", f"opt-in stored with time + wording version ({st})")
    check(not nl.list_path(subs).exists(), "a TEST-tier submission never writes the list")
    st, resp = submit(**NEW)
    check(record(resp)["form"].get("newsletter_opt_in") is None, "box absent -> None (older form / unticked)")
    st, resp = submit(**NEW, newsletter_opt_in="")
    check(record(resp)["form"].get("newsletter_opt_in") is False, "box sent empty -> False")

    row = nl.subscribe(subs, email="Author@Example.com", name="A. Author", sub_id="ICSAC-SUB-00042",
                       wording_version="2026-09-30", consented_at="2026-09-29T05:00:00Z")
    check(row["email"] == "author@example.com" and nl.list_path(subs).exists(), "subscribe appends, lower-cases")
    check(nl.is_subscribed(subs, "author@example.com"), "is_subscribed")
    nl.subscribe(subs, email="b@example.com", name="B", sub_id="ICSAC-SUB-00043", wording_version="2026-09-30")
    check([r["email"] for r in nl.current_list(subs)] == ["author@example.com", "b@example.com"], "current_list")
    tok = nl.unsubscribe_token(b"consents-test-secret", "author@example.com")
    check(nl.email_from_token(b"consents-test-secret", tok) == "author@example.com", "token round-trips")
    check(nl.email_from_token(b"other-secret", tok) is None and nl.email_from_token(b"consents-test-secret", tok[:-1] + "0") is None,
          "a forged or foreign token yields nothing")
    r = client.get("/api/newsletter/unsubscribe", params={"t": tok})
    check(r.status_code == 200 and r.json().get("ok") is True, f"unsubscribe endpoint 200 ({r.status_code})")
    check([x["email"] for x in nl.current_list(subs)] == ["b@example.com"], "folded list drops the unsubscribed address")
    check(len(nl.events(subs)) == 3, "events are append-only (3 rows)")
    r = client.get("/api/newsletter/unsubscribe", params={"t": "nonsense"})
    check(r.status_code == 404, f"bad token -> 404 ({r.status_code})")
    check(all(e.get("event") != "newsletter_subscribed" for e in audit), "no production subscribe event from T2 posts")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAIL: {len(failures)} failure(s)")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
