#!/usr/bin/env python3
"""Regression checks for the audit of 2026-09-29.
Offline: no network, no mail, no pings; every write lands in one temporary
directory that is removed at the end, pass or fail. Run from the repo root:
    .venv/bin/python intake/scripts/test_audit_20260929.py

Covers: a repeated identity field is refused at intake; creator and related-
identifier lists are bounded; author text cannot inject markup into the HTML
part of a mail; the decision mail names the PDFs that are really attached; a
registry outage is retried before an author hears anything; a decided paper is
not reprocessed by a stray queue trigger; a decision needs a paper that is
awaiting one; state.json is written atomically; register --live refuses a
paper whose acceptance mail never left; a sponsor session whose subscription
ended cannot change the listing; the arXiv download has a byte ceiling; no
active template says "human curator".
"""
from __future__ import annotations

import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def raises(fn, exc_type, needle: str = "") -> bool:
    try:
        fn()
    except exc_type as exc:  # noqa: PERF203
        return needle in str(getattr(exc, "detail", "") or "") + str(exc)
    except Exception as exc:  # noqa: BLE001
        print(f"       unexpected {type(exc).__name__}: {exc}")
        return False
    return False


tmp = Path(tempfile.mkdtemp(prefix="icsac-audit-20260929-"))
try:
    from fastapi import HTTPException
    from starlette.datastructures import FormData

    # ── intake: one value per identity field; bounded lists ─────────────────
    from intake import intake_server as srv
    dup = FormData([("orcid", "0000-0002-1825-0097"), ("orcid", "0000-0001-5109-3700"), ("name", "A")])
    check(raises(lambda: srv._reject_duplicate_fields(dup, ("orcid", "name", "email")), HTTPException, "orcid was sent more than once"),
          "a body with two orcid fields is refused with 400")
    single = FormData([("orcid", "0000-0002-1825-0097"), ("name", "A"), ("email", "a@example.com")])
    srv._reject_duplicate_fields(single, ("orcid", "name", "email"))
    check(True, "a body with one value per identity field passes")
    check(raises(lambda: srv._parse_creators(json.dumps([{"name": "Author, Test"}] * (srv.MAX_CREATORS + 1))), HTTPException, "at most"),
          f"{srv.MAX_CREATORS + 1} creators are refused")
    check(len(srv._parse_creators(json.dumps([{"name": "Author, Test"}] * 3))) == 3, "3 creators parse")
    check(raises(lambda: srv._parse_related_identifiers(json.dumps([{"identifier": "10.5281/zenodo.1", "relation": "isSupplementTo"}] * (srv.MAX_RELATED_IDENTIFIERS + 1))), HTTPException, "at most"),
          f"{srv.MAX_RELATED_IDENTIFIERS + 1} related identifiers are refused")

    # ── mail: author text is text in the HTML part ───────────────────────────
    import email_send
    html = email_send._markdown_to_html("Dear <b>x</b>, your paper *Title <img src=x onerror=alert(1)>* was read.")
    check("<img src=x" not in html and "<b>x</b>" not in html and "&lt;img src=x" in html, "a '<' from a substituted value renders as text, not a tag")
    check("<em>Title" in html, "markdown emphasis still renders")

    # ── mail: attachments named are attachments sent ─────────────────────────
    from intake import notify_author as na
    check(na._attachments_sentence("ICSAC-SUB-00001", "panel", "rqc").startswith("Two PDFs are attached"), "both PDFs -> 'Two PDFs'")
    check(na._attachments_sentence("ICSAC-SUB-00001", "panel", "").startswith("One PDF is attached"), "panel only -> 'One PDF'")
    check(na._attachments_sentence("ICSAC-SUB-00001", "", "").startswith("No PDF is attached"), "nothing -> 'No PDF'")
    check(na._attachments_block("ICSAC-SUB-00001", "panel", "rqc").count("\n- ") == 2, "block lists two files when both exist")
    check("icsac-rqc" not in na._attachments_block("ICSAC-SUB-00001", "panel", ""), "block omits the RQC file when it was not produced")
    tdir = ROOT / "intake" / "templates"
    for t in ("submission_accept_upload", "submission_accept_doi", "submission_revise_doi", "submission_revise_upload",
              "submission_scope_reject_doi", "submission_scope_reject_upload"):
        check("{{attachments_block}}" in (tdir / f"{t}.md").read_text() and "Two PDFs are attached" not in (tdir / f"{t}.md").read_text(),
              f"{t}.md uses the attachments block")
    check("{{attachments_sentence}}" in (tdir / "submission_accept_upload_pending.md").read_text(), "accept_upload_pending uses the attachments sentence")

    # ── worker: transient failures retry; decided papers are not reprocessed; atomic state ──
    from intake import submission_worker as worker
    check(worker._is_transient(urllib.error.HTTPError("u", 503, "x", {}, None)), "HTTP 503 is transient")
    check(not worker._is_transient(urllib.error.HTTPError("u", 404, "x", {}, None)), "HTTP 404 is not")
    check(worker._is_transient(urllib.error.URLError("timed out")), "URLError is transient")
    worker._RETRY_SLEEPS = (0, 0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("down")
        return {"ok": True}
    check(worker._fetch_with_retry(flaky, "test") == {"ok": True} and calls["n"] == 3, "two outages then success: the third try returns")
    check(raises(lambda: worker._fetch_with_retry(lambda: (_ for _ in ()).throw(ValueError("bad record")), "test"), ValueError, "bad record"),
          "a non-transient error is raised at once")

    sub = tmp / "ICSAC-SUB-00097"
    sub.mkdir()
    (sub / "submission.json").write_text(json.dumps({"title": "T"}))
    (sub / "state.json").write_text(json.dumps({"state": "completed", "decision": "accept", "completed_at": "2026-09-01T00:00:00Z"}))
    audited: list[dict] = []
    worker._resolve_sub_dir = lambda sid: (sub, 1)
    worker._audit = lambda e, **kw: audited.append(e)
    os.environ.pop("ICSAC_REPROCESS", None)
    worker._process_locked("ICSAC-SUB-00097")
    st = json.loads((sub / "state.json").read_text())
    check(any(e.get("event") == "reprocess_refused" for e in audited) and st["state"] == "completed" and st["decision"] == "accept",
          "a completed, decided paper is refused by the worker and its state is untouched")
    worker._write_state(sub, note="atomic")
    check(json.loads((sub / "state.json").read_text())["note"] == "atomic" and not (sub / "state.json.tmp").exists(), "worker state write is atomic (no .tmp left)")
    from intake import author_approval as aa
    aa._update_state(sub, other=1)
    check(json.loads((sub / "state.json").read_text())["other"] == 1 and not (sub / "state.json.tmp").exists(), "author_approval state write is atomic")

    # ── decision gate: a paper must be awaiting a decision ───────────────────
    from intake import apply_decision as ad
    ad.SUBMISSIONS_ROOT = tmp
    ad.TEST_SUBMISSIONS_ROOT = tmp / "test"
    sub2 = tmp / "ICSAC-SUB-00095"
    sub2.mkdir()
    (sub2 / "submission.json").write_text(json.dumps({"title": "T", "source": "upload", "tier": 1,
                                                       "form": {"name": "Test Author", "email": "author@example.com", "orcid": "0000-0002-1825-0097"}}))
    (sub2 / "state.json").write_text(json.dumps({"state": "in_review"}))
    os.environ.pop("ICSAC_DECISION_FORCE", None)
    rc = ad.main(["apply_decision.py", "ICSAC-SUB-00095", "revise", "note"])
    check(rc == 3 and json.loads((sub2 / "state.json").read_text()).get("decision") is None,
          "revise on a paper still in review is refused (exit 3) and nothing is recorded")

    # ── register --live refuses when the acceptance mail never left ──────────
    import crossref_deposit as cd
    sub3 = tmp / "ICSAC-SUB-00096"
    (sub3 / "crossref").mkdir(parents=True)
    (sub3 / "crossref" / "deposit.xml").write_bytes(b"<x/>")
    (sub3 / "crossref" / "staged.json").write_text(json.dumps({"doi": "10.67697/icsac.2026.999"}))
    (sub3 / "submission.json").write_text(json.dumps({"title": "T"}))
    (sub3 / "state.json").write_text(json.dumps({"state": "completed_email_failed", "decision": "accept"}))
    os.environ.pop("ICSAC_REGISTER_WITHOUT_NOTICE", None)
    check(raises(lambda: cd.register(sub3, live=True, log=lambda m: None), cd.CrossrefError, "failed to draft"),
          "register --live refuses a paper whose acceptance email failed to draft")
    check("_write_json_atomic" in inspect.getsource(cd.register), "register writes state.json atomically")

    # ── sponsor: an ended subscription cannot change the listing ─────────────
    from intake import sponsor_logo as sl
    session = {"mode": "subscription", "payment_status": "paid",
               "subscription": {"id": "sub_x", "status": "canceled", "current_period_end": 1_700_000_000,
                                "items": {"data": [{"price": {"product": sl.SPONSOR_PRODUCT_ID}}]}},
               "custom_fields": [{"key": "display_name", "text": {"value": "X"}}, {"key": "website_url", "text": {"value": "https://example.com"}}]}
    check(raises(lambda: sl._verify_sponsor_session(session), HTTPException, "canceled"), "a canceled sponsor subscription is refused (403)")
    session["subscription"]["status"] = "active"
    check(raises(lambda: sl._verify_sponsor_session(session), HTTPException, "period has ended"), "an active flag with a period that ended long ago is refused")

    # ── arXiv download byte ceiling ──────────────────────────────────────────
    import submission_intake as ingest

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    real_urlopen = ingest.urllib.request.urlopen
    ingest.urllib.request.urlopen = lambda req, timeout=0: _Resp(b"%PDF-" + b"x" * 300_000)
    os.environ["INTAKE_MAX_PDF_BYTES"] = "100000"
    try:
        out = ingest.download_arxiv_pdf("2401.00001", dest_dir=str(tmp))
    finally:
        ingest.urllib.request.urlopen = real_urlopen
        os.environ.pop("INTAKE_MAX_PDF_BYTES", None)
    check(out is None and not list(tmp.glob("2401.00001*")), "an arXiv PDF over the ceiling is refused and removed")

    # ── wording ──────────────────────────────────────────────────────────────
    offenders = []
    for d in (ROOT / "templates", ROOT / "intake" / "templates"):
        for f in d.glob("*.md"):
            if "human curator" in f.read_text().lower():
                offenders.append(f.name)
    check(not offenders, f"no active template says 'human curator' ({offenders})")
    import redaction
    check("human curators" not in inspect.getsource(redaction).lower(), "the RQC boilerplate says curation team")

    # ── public tooling: no production path baked in ──────────────────────────
    check("/home/orangepi" not in (ROOT / "tools" / "wrap-paper-pdf.py").read_text(), "wrap-paper-pdf.py carries no home path")
    check("httpx" in (ROOT / "requirements.txt").read_text(), "httpx is a declared dependency")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
