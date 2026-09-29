#!/usr/bin/env python3
"""Checks for the publish gate (the curator's rule, 2026-09-29: nothing publishes
anywhere without his approval; everything else is drafted and ready to go).
Offline: git and the network are stubbed; every write lands in one temporary
directory that is removed at the end, pass or fail. Run from the repo root:
    .venv/bin/python intake/scripts/test_publish_gate.py

Covers: the registry, the public-review stage and the push all refuse without
approval and save the entry as ready to publish; register --live carries the
approval; the Zenodo watcher holds a newly published record, pings the curator
once, and never tells the author; the Zenodo-community accept holds before it
touches the site; the legacy DOI-route accept returns nothing to publish; no
service unit or cron line sets the approval.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
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


os.environ.pop("ICSAC_PUBLISH_APPROVED", None)
tmp = Path(tempfile.mkdtemp(prefix="publish-gate-"))
try:
    import publications as pub

    site = tmp / "site"
    (site / "src" / "data").mkdir(parents=True)
    reg = site / "src" / "data" / "accepted.json"
    reg.write_text("[]")
    pub.WEBSITE_REPO = str(site)
    pub.REGISTRY_PATH = str(reg)
    pub.READY_DIR = str(tmp / "ready")
    git_calls: list = []
    real_run = subprocess.run
    pub.subprocess.run = lambda cmd, **k: (git_calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]

    proto = {"title": "A held paper", "authors": ["A. Author"], "doi": "10.67697/icsac.2026.990",
             "source": "submission-pdf", "sub_id": "ICSAC-SUB-00990"}

    print("1. the writers refuse without approval")
    check(pub.upsert_entry(dict(proto)) == {}, "registry: no entry without approval")
    check(json.loads(reg.read_text()) == [], "registry: accepted.json untouched")
    ready = Path(pub.READY_DIR) / "ICSAC-SUB-00990.json"
    check(ready.exists() and json.loads(ready.read_text())["proto"]["doi"] == proto["doi"],
          "the entry is saved as ready to publish")
    check(pub.stage_public_review_for_slug("ICSAC-SUB-00990", "a-held-paper", str(tmp)) == (None, None)
          and not (site / "src" / "data" / "public-reviews").exists(), "public review: nothing staged")
    pub.commit_and_push("publications: held")
    check(git_calls == [], "push: no git command runs")

    print("2. approval opens it")
    e = pub.upsert_entry(dict(proto), approved=True)
    check(bool(e.get("slug")) and len(json.loads(reg.read_text())) == 1, "approved=True writes the entry")
    pub.commit_and_push("publications: approved", approved=True)
    check(any(c[:2] == ("git", "add") for c in git_calls), "approved=True pushes")
    os.environ["ICSAC_PUBLISH_APPROVED"] = "1"
    check(pub.publish_approved(), "a hand-run with ICSAC_PUBLISH_APPROVED=1 is approved")
    os.environ.pop("ICSAC_PUBLISH_APPROVED")
    check(not pub.publish_approved(), "without it, not approved")

    print("3. register --live is the approved path")
    import crossref_deposit as cd
    src = inspect.getsource(cd._push_publications)
    check(src.count("approved=True") == 3, "_push_publications passes approval to all three writers")

    print("4. the Zenodo watcher holds, pings once, tells no author")
    import publish_watcher as pw
    sub_dir = tmp / "subs" / "ICSAC-SUB-00991"
    sub_dir.mkdir(parents=True)
    (sub_dir / "state.json").write_text(json.dumps({"deposit_record_id": "991"}))
    (sub_dir / "submission.json").write_text(json.dumps({"title": "W", "creators": [{"name": "Doe, Jo"}],
                                                          "form": {"email": "a@example.com", "name": "Jo Doe"}}))
    pw._list_awaiting_publish = lambda: [sub_dir]
    pw._get_deposit = lambda rid: {"submitted": True, "state": "done", "doi": "10.5281/zenodo.991",
                                   "record_id": 991, "metadata": {"title": "W"}}
    told = []
    pw._notify_author_published = lambda *a, **k: told.append(a)
    import notify
    pings = []
    notify.send_to_curator = lambda msg, **k: pings.append(msg)
    s1 = pw.poll_drafts()
    st = json.loads((sub_dir / "state.json").read_text())
    check(s1.get("held") == 1 and s1.get("published") == 0, "the record is held, not published")
    check(len(pings) == 1 and "READY TO PUBLISH" in pings[0], "the curator is told once")
    check(told == [] and st.get("state") != "published" and st.get("publish_held_at"),
          "no author email, state not marked published")
    s2 = pw.poll_drafts()
    check(s2.get("held") == 1 and len(pings) == 1, "the next tick stays quiet")

    print("5. the Zenodo-community accept holds before touching the site")
    import action
    action._fetch_record = lambda rid: {"metadata": {}}
    action._extract_registry_entry = lambda rid, md, source: {"title": "C", "authors": ["C"], "doi": "10.5281/zenodo.992",
                                                               "source": source, "record_id": rid}
    touched = []
    action._publish_public_review = lambda rid: touched.append("review")
    action._refresh_panel_stats = lambda: touched.append("stats")
    before = reg.read_text()
    action.register_accepted_paper("992")
    check(touched == [] and reg.read_text() == before, "no review, no stats, no registry write")
    check((Path(pub.READY_DIR) / "992.json").exists() and any("992" in p for p in pings),
          "saved as ready to publish, and the curator is told")

    print("6. the legacy DOI-route accept")
    from intake import submission_worker as sw
    src = inspect.getsource(sw._register_doi_accept)
    check("if not entry:" in src and "return None" in src, "a held entry returns nothing to publish")

    print("7. nothing automatic carries the approval")
    units = list(Path("/etc/systemd/system").glob("*.service"))
    check(all("ICSAC_PUBLISH_APPROVED" not in u.read_text(errors="replace") for u in units),
          f"no service unit sets ICSAC_PUBLISH_APPROVED ({len(units)} units read)")
    try:
        cron = real_run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        cron = ""
    check("ICSAC_PUBLISH_APPROVED" not in cron, "no cron line sets it")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
