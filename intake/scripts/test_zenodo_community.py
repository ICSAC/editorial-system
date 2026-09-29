#!/usr/bin/env python3
"""Checks for the ICSAC Zenodo community step of register --live (his yes,
2026-09-29): the inclusion request that publishing opens is accepted as part
of the approved publish; a record already in the community is left alone; a
failure never stops the run and a re-run tries again. Offline: the Zenodo API
and every other network step are stubbed; writes land in one temporary
directory that is removed at the end, pass or fail. Run from the repo root:
    .venv/bin/python intake/scripts/test_zenodo_community.py
"""
from __future__ import annotations

import json
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


tmp = Path(tempfile.mkdtemp(prefix="zenodo-community-"))
try:
    import repository_deposit as rd

    COMM = "130c51d8-icsac"
    calls: list = []

    def fake(members=(), requests=(), accept_link=True):
        def _req(method, url, *, token, body=None, **k):
            calls.append((method, url))
            if url.endswith("/communities/icsac"):
                return {"id": COMM}
            if url.endswith("/communities"):
                return {"hits": {"hits": [{"id": c} for c in members]}}
            if url.endswith("/requests"):
                hits = []
                for rid, status in requests:
                    links = {"actions": {"accept": f"https://zenodo.org/api/requests/{rid}/actions/accept"}} if accept_link else {}
                    hits.append({"id": rid, "type": "community-inclusion", "status": status,
                                 "receiver": {"community": COMM}, "links": links})
                return {"hits": {"hits": hits}}
            if "/actions/accept" in url:
                return {"status": "accepted"}
            raise AssertionError(url)
        return _req

    print("1. the helper")
    calls.clear(); rd._request_json = fake(requests=[("r1", "submitted")])
    check(rd.accept_community_inclusion("9") == "accepted"
          and ("POST", "https://zenodo.org/api/requests/r1/actions/accept") in calls,
          "a pending request is accepted")
    calls.clear(); rd._request_json = fake(members=[COMM])
    check(rd.accept_community_inclusion("9") == "already" and not any(m == "POST" for m, _ in calls),
          "a record already in the community is left alone")
    calls.clear(); rd._request_json = fake(requests=[("r2", "accepted")])
    check(rd.accept_community_inclusion("9") == "none" and not any(m == "POST" for m, _ in calls),
          "no pending request, nothing done")
    calls.clear(); rd._request_json = fake(requests=[("r3", "submitted")], accept_link=False)
    check(rd.accept_community_inclusion("9") == "not-permitted", "a token that may not accept says so")

    print("2. the live run")
    import crossref_deposit as cd
    from intake import author_approval
    sub = tmp / "ICSAC-SUB-09998"
    (sub / "crossref").mkdir(parents=True)
    (sub / "submission.json").write_text(json.dumps({"sub_id": sub.name, "title": "T", "source": "upload"}))
    (sub / "state.json").write_text(json.dumps({"decision": "accept", "state": "completed", "deposit_record_id": "555"}))
    (sub / "crossref/deposit.xml").write_bytes(b"<x/>")
    (sub / "crossref/staged.json").write_text(json.dumps({"doi": "10.67697/icsac.2026.998", "registered": False}))
    cd.stage = lambda *a, **k: None
    cd.validate_xml = lambda b: None
    cd.deposit = lambda *a, **k: "batch-1"
    cd.poll_result = lambda *a, **k: {"status": "completed", "success": 1, "failure": 0}
    pings: list = []
    cd._ping = lambda msg: pings.append(msg)
    cd._push_publications = lambda *a, **k: {"slug": "t"}
    cd._publish_zenodo_archive = lambda *a, **k: "https://zenodo.org/record/555"
    cd._notify_author = lambda *a, **k: True
    author_approval.gate = lambda *a, **k: (True, "approved (stub)")
    seen: list = []

    def flaky(record_id, *, log):
        seen.append(record_id)
        return "failed: HTTP 503" if len(seen) == 1 else "accepted"
    cd._accept_zenodo_community = flaky
    out = cd.register(sub, live=True, log=lambda m: None)
    ck = json.loads((sub / "crossref/staged.json").read_text())["checkpoints"]
    check(seen == ["555"] and out.get("community", "").startswith("failed")
          and "zenodo_community_at" not in ck, "a failure is reported, not recorded as done")
    check(bool(ck.get("author_drafted_at")) and "ICSAC Zenodo community: failed" in pings[-1],
          "the run still finishes, and the ping says what happened")
    out = cd.register(sub, live=True, log=lambda m: None)
    ck = json.loads((sub / "crossref/staged.json").read_text())["checkpoints"]
    check(seen == ["555", "555"] and ck.get("zenodo_community") == "accepted" and ck.get("zenodo_community_at"),
          "a re-run tries again and records the accept")
    out = cd.register(sub, live=True, log=lambda m: None)
    check(seen == ["555", "555"], "once accepted, a later run leaves it alone")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
