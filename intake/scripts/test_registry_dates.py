#!/usr/bin/env python3
"""Checks the two dates of a registry entry: accepted_date (the day of
acceptance) and published_date (the day of online publication). register
--live writes both from the deposited Crossref XML, so the page shows the
dates Crossref holds; a date missing there falls back to the accept
decision's stamp (accepted) or today (published), and the log says so; an
existing entry keeps its dates on a re-run.
Offline: no network, no mail, no pings; every write lands in one temporary
directory that is removed at the end, pass or fail. Run from the repo root
with the venv:
    .venv/bin/python intake/scripts/test_registry_dates.py
"""
from __future__ import annotations

import datetime
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def deposit_xml(published: str | None, accepted: str | None) -> str:
    """A minimal deposit: the issue's year-only date, then the article's dates."""
    def d(tag: str, iso: str | None, attrs: str = "") -> str:
        if not iso:
            return ""
        y, m, dd = iso.split("-")
        return f"<{tag}{attrs}><month>{m}</month><day>{dd}</day><year>{y}</year></{tag}>"
    online = ' media_type="online"'
    return ('<?xml version="1.0"?>\n<doi_batch xmlns="http://www.crossref.org/schema/5.4.0"><body><journal>'
            '<journal_issue><publication_date media_type="online"><year>2026</year></publication_date></journal_issue>'
            f'<journal_article>{d("publication_date", published, online)}'
            f'{d("acceptance_date", accepted)}</journal_article></journal></body></doi_batch>')


tmp = Path(tempfile.mkdtemp(prefix="registry-dates-"))
try:
    import publications as pub
    import crossref_deposit as cd
    from intake import author_approval

    site = tmp / "site"; (site / "src" / "data").mkdir(parents=True)
    reg = site / "src" / "data" / "accepted.json"
    pub.WEBSITE_REPO = str(site); pub.REGISTRY_PATH = str(reg)
    pub.READY_DIR = str(tmp / "ready-to-publish")   # held entries stay inside the temp dir

    def entries() -> list[dict]:
        return json.loads(reg.read_text())

    # ── 1. the registry keeps both dates ─────────────────────────────────────
    print("1. registry")
    reg.write_text("[]")
    base = {"title": "T", "authors": ["A. Author"], "doi": "10.5555/icsac.2026.980", "source": "submission-pdf"}
    e = pub.upsert_entry(dict(base, accepted_date="2026-03-02", published_date="2026-03-03"), approved=True)
    check((e.get("accepted_date"), e.get("published_date")) == ("2026-03-02", "2026-03-03"), "upsert writes both dates")
    keys = list(e)
    check(keys.index("published_date") == keys.index("accepted_date") + 1, "published_date sits right after accepted_date")
    e = pub.upsert_entry(dict(base), approved=True)
    check((e.get("accepted_date"), e.get("published_date")) == ("2026-03-02", "2026-03-03"),
          "a later upsert without dates keeps both")
    before = reg.read_text()
    try:
        pub.upsert_entry(dict(base, published_date="2026-03-01"), approved=True)
        refused = False
    except ValueError:
        refused = True
    check(refused and reg.read_text() == before, "published before accepted: ValueError, registry unchanged")
    e = pub.upsert_entry(dict(base, doi="10.5555/icsac.2026.981", accepted_date="2026-03-02"), approved=True)
    check("published_date" not in e, "an entry given no published_date gets none (the page then reads accepted_date)")
    check(pub.find_entry({"doi": "10.5555/icsac.2026.980"}).get("published_date") == "2026-03-03"
          and pub.find_entry({"doi": "10.5555/icsac.2026.999"}) is None, "find_entry: the entry an upsert would update, else None")

    # ── 2. the acceptance day is the decision stamp's UTC date ───────────────
    print("2. decision stamp")
    sd = tmp / "stamp"; sd.mkdir()
    (sd / "state.json").write_text(json.dumps({"decision": "accept", "completed_at": "2026-02-10T23:30:00Z"}))
    check(str(cd._decision_date(sd)) == "2026-02-10", "an accept stamp gives its UTC date")
    (sd / "state.json").write_text(json.dumps({"decision": "revise", "completed_at": "2026-02-10T23:30:00Z"}))
    check(cd._decision_date(sd) is None, "a stamp on another decision gives none")
    (sd / "state.json").unlink()
    check(cd._decision_date(sd) is None, "no state.json gives none")

    # ── 3. register --live writes the dates from the deposit ─────────────────
    print("3. register --live")
    sys.modules["stats"] = types.SimpleNamespace(
        compute_stats=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stats stubbed")))
    pub.stage_public_review_for_slug = lambda *a, **k: (None, None)
    pub.commit_and_push = lambda *a, **k: None
    cd.stage = lambda *a, **k: None
    cd.validate_xml = lambda b: None
    cd.deposit = lambda *a, **k: "batch-1"
    cd.poll_result = lambda *a, **k: {"status": "completed", "success": 1, "failure": 0}
    cd._ping = lambda *a, **k: None
    cd._publish_zenodo_archive = lambda *a, **k: None
    cd._notify_author = lambda *a, **k: True
    author_approval.gate = lambda *a, **k: (True, "approved (stub)")

    def make_sub(name: str, doi: str, xml: str, state: dict) -> Path:
        sub = tmp / name
        (sub / "crossref").mkdir(parents=True)
        (sub / "submission.json").write_text(json.dumps({
            "sub_id": name, "title": f"Article {name}", "abstract": "A", "license": "cc-by-4.0",
            "creators": [{"name": "Lovelace, Ada"}], "form": {"name": "Ada Lovelace"}}))
        (sub / "state.json").write_text(json.dumps(state))
        (sub / "crossref" / "deposit.xml").write_text(xml)
        (sub / "crossref" / "staged.json").write_text(json.dumps({"doi": doi, "registered": False}))
        return sub

    def entry_for(sub_id: str) -> dict:
        return next(x for x in entries() if x.get("sub_id") == sub_id)

    reg.write_text("[]")
    accept = {"decision": "accept", "state": "completed", "completed_at": "2026-02-10T23:30:00Z"}
    logs: list[str] = []
    sub = make_sub("ICSAC-SUB-09980", "10.5555/icsac.2026.982", deposit_xml("2026-03-03", "2026-03-02"), accept)
    cd.register(sub, live=True, log=logs.append)
    e = entry_for("ICSAC-SUB-09980")
    check((e.get("accepted_date"), e.get("published_date")) == ("2026-03-02", "2026-03-03"),
          "both dates are the deposit's (not the stamp's, not today)")
    said = [m for m in logs if "registry dates:" in m]
    check(said and "accepted 2026-03-02 (from the deposit)" in said[-1]
          and "published 2026-03-03 (from the deposit)" in said[-1], "the log names the deposit as the source of each")

    # ── 4. a date missing from the deposit falls back, and the log says so ───
    print("4. fallback")
    today = datetime.date.today().isoformat()
    logs.clear()
    sub = make_sub("ICSAC-SUB-09981", "10.5555/icsac.2026.983", deposit_xml(None, None), accept)
    cd.register(sub, live=True, log=logs.append)
    e = entry_for("ICSAC-SUB-09981")
    check(e.get("accepted_date") == "2026-02-10", "accepted falls back to the decision stamp's date")
    check(e.get("published_date") in (today, datetime.date.today().isoformat()), "published falls back to today")
    said = [m for m in logs if "registry dates:" in m]
    check(said and "accepted 2026-02-10 (NOT in the deposit; the accept decision's stamp)" in said[-1]
          and "(NOT in the deposit; today)" in said[-1], "the log says which date fell back, and to what")
    logs.clear()
    sub = make_sub("ICSAC-SUB-09982", "10.5555/icsac.2026.984", deposit_xml(None, None),
                   {"decision": "accept", "state": "completed"})
    cd.register(sub, live=True, log=logs.append)
    said = [m for m in logs if "registry dates:" in m]
    check(said and f"accepted {entry_for('ICSAC-SUB-09982').get('accepted_date')} (NOT in the deposit; today)" in said[-1],
          "no stamp either: accepted is today, and the log says so")
    logs.clear()
    sub = make_sub("ICSAC-SUB-09983", "10.5555/icsac.2026.985", deposit_xml("2026-03-01", "2026-03-02"), accept)
    cd.register(sub, live=True, log=logs.append)
    e = entry_for("ICSAC-SUB-09983")
    said = [m for m in logs if "registry dates:" in m]
    check(e.get("accepted_date") == "2026-03-02" and "published_date" not in e
          and said and "published 2026-03-01 DROPPED: before acceptance" in said[-1],
          "a deposited publication before acceptance is dropped, loudly, and the page still publishes")

    # ── 5. a re-run keeps an existing entry's dates ──────────────────────────
    print("5. re-run")

    class LaterDay(datetime.date):
        @classmethod
        def today(cls):
            return datetime.date(2026, 6, 1)

    def rerun(sub: Path) -> None:
        """A crash between the page push and its checkpoint: register runs the landing step again."""
        sp = sub / "crossref" / "staged.json"
        st = json.loads(sp.read_text())
        st.get("checkpoints", {}).pop("publications_pushed_at", None)
        sp.write_text(json.dumps(st))
        real_dt = cd._dt
        cd._dt = types.SimpleNamespace(date=LaterDay, datetime=datetime.datetime, timezone=datetime.timezone)
        try:
            cd.register(sub, live=True, log=logs.append)
        finally:
            cd._dt = real_dt

    first = entry_for("ICSAC-SUB-09981")
    logs.clear()
    rerun(tmp / "ICSAC-SUB-09981")
    again = entry_for("ICSAC-SUB-09981")
    check(any("registry dates:" in m for m in logs), "the re-run reached the landing step")
    check((again.get("accepted_date"), again.get("published_date"))
          == (first.get("accepted_date"), first.get("published_date")),
          "fallback dates survive a re-run on a later day")
    said = [m for m in logs if "registry dates:" in m]
    check(said and "the existing entry's, kept" in said[-1], "the log says the existing dates were kept")
    logs.clear()
    rerun(tmp / "ICSAC-SUB-09980")
    again = entry_for("ICSAC-SUB-09980")
    check((again.get("accepted_date"), again.get("published_date")) == ("2026-03-02", "2026-03-03"),
          "deposited dates are the same on a re-run")
    check(len([x for x in entries() if x.get("sub_id") == "ICSAC-SUB-09980"]) == 1, "a re-run updates, never duplicates")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
