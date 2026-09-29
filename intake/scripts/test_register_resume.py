#!/usr/bin/env python3
"""Checks for the live registration's landing-page step (2026-09-29): a push that did
not happen is never recorded as done, the Crossref checkpoint survives, and a re-run
resumes at the page step without depositing again; the operator CLI names the website
repo. Offline: every network step is stubbed; writes land in one temporary directory
that is removed at the end, pass or fail. Run from the repo root:
    .venv/bin/python intake/scripts/test_register_resume.py
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


tmp = Path(tempfile.mkdtemp(prefix="register-resume-"))
try:
    import crossref_deposit as cd
    from intake import author_approval

    sub = tmp / "ICSAC-SUB-09999"
    (sub / "crossref").mkdir(parents=True)
    (sub / "submission.json").write_text(json.dumps({"sub_id": sub.name, "title": "T", "source": "upload"}))
    (sub / "state.json").write_text(json.dumps({"decision": "accept", "state": "completed"}))
    (sub / "crossref/deposit.xml").write_bytes(b"<x/>")
    (sub / "crossref/staged.json").write_text(json.dumps({"doi": "10.67697/icsac.2026.999", "registered": False}))

    calls = {"deposit": 0, "stage": 0}
    cd.stage = lambda *a, **k: calls.__setitem__("stage", calls["stage"] + 1)
    cd.validate_xml = lambda b: None
    def _dep(*a, **k):
        calls["deposit"] += 1
        return "batch-1"
    cd.deposit = _dep
    cd.poll_result = lambda *a, **k: {"status": "completed", "success": 1, "failure": 0}
    cd._ping = lambda msg: None
    cd._publish_zenodo_archive = lambda *a, **k: None
    cd._notify_author = lambda *a, **k: True
    author_approval.gate = lambda *a, **k: (True, "approved (stub)")

    print("1. the page push does not happen")
    cd._push_publications = lambda *a, **k: {}
    try:
        cd.register(sub, live=True, log=lambda m: None)
        raised = ""
    except cd.CrossrefError as exc:
        raised = str(exc)
    check("NOT pushed" in raised and "resume" in raised, "the live run stops with a message that says how to resume")
    ck = json.loads((sub / "crossref/staged.json").read_text()).get("checkpoints", {})
    check(bool(ck.get("crossref_registered_at")), "the Crossref checkpoint is kept")
    check("publications_pushed_at" not in ck, "the page step is not recorded as done")
    check(calls["deposit"] == 1, "Crossref was deposited once")

    print("2. the re-run")
    cd._push_publications = lambda *a, **k: {"slug": "t"}
    out = cd.register(sub, live=True, log=lambda m: None)
    ck = json.loads((sub / "crossref/staged.json").read_text()).get("checkpoints", {})
    check(calls["deposit"] == 1, "the re-run does not deposit again")
    check(ck.get("publications_slug") == "t" and bool(ck.get("publications_pushed_at")), "the page step completes")
    check(bool(ck.get("author_drafted_at")) and out.get("author_draft") == "drafted", "the later steps run")

    print("3. the operator CLI")
    sh = (ROOT / "intake" / "register-doi.sh").read_text()
    check('ICSAC_WEBSITE_REPO="${ICSAC_WEBSITE_REPO:-$HOME/Desktop/icsac/icsacinstitute.org}"' in sh
          and sh.index("ICSAC_WEBSITE_REPO=") < sh.index("set +a"),
          "register-doi.sh exports the website checkout for the /publications push")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
