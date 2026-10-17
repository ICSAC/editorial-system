#!/usr/bin/env python3
"""Checks that a paper's keywords reach its public record (2026-10-02).
Offline: no network, no mail, no pings; every write lands in one temporary
directory that is removed at the end, pass or fail. Run from the repo root
with the venv:
    .venv/bin/python intake/scripts/test_keywords_on_record.py

Covers: the cleaner; the registry keeping keywords; each of the four registry
writers passing them (Zenodo community accept, DOI-route accept, Zenodo-route
publish, Crossref-route register); the DOI route's optional form field winning
over the source record; the backfill tool's sources and its write gate.
"""
from __future__ import annotations

import importlib.util
import json
import os
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


tmp = Path(tempfile.mkdtemp(prefix="keywords-"))
try:
    import publications as pub
    pub.READY_DIR = str(tmp / "ready-to-publish")   # held entries stay inside the temp dir

    # ── 1. the cleaner ───────────────────────────────────────────────────────
    print("1. cleaner")
    check(pub.clean_keywords(None) == [], "None -> []")
    check(pub.clean_keywords("alpha, beta; gamma") == ["alpha", "beta", "gamma"], "string split on , and ;")
    check(pub.clean_keywords(["alpha", "Alpha", " beta. ", ""]) == ["alpha", "beta"],
          "trimmed, trailing full stop dropped, case-insensitive duplicate dropped")
    check(pub.clean_keywords(["one, two", "three"]) == ["one", "two", "three"], "a pasted list inside one item is split")
    check(pub.clean_keywords(["x" * 81, "ok"]) == ["ok"], "an item over 80 characters is dropped")
    check(len(pub.clean_keywords([f"k{i}" for i in range(20)])) == pub.KEYWORD_LIMIT, "at most KEYWORD_LIMIT kept")
    check(pub.clean_keywords(["en–dash term", "agent–environment"]) == ["en–dash term", "agent–environment"],
          "non-ASCII punctuation kept as typed")

    # ── 2. the registry keeps them ───────────────────────────────────────────
    print("2. registry")
    site = tmp / "site"; (site / "src" / "data").mkdir(parents=True)
    reg = site / "src" / "data" / "accepted.json"
    reg.write_text("[]")
    pub.WEBSITE_REPO = str(site); pub.REGISTRY_PATH = str(reg)
    e = pub.upsert_entry(approved=True, proto={"title": "T", "authors": ["A"], "doi": "10.5555/icsac.2026.990",
                                                "source": "submission-pdf", "keywords": ["alpha", "beta"]})
    check(e.get("keywords") == ["alpha", "beta"], "upsert writes keywords")
    e = pub.upsert_entry(approved=True, proto={"title": "T", "authors": ["A"], "doi": "10.5555/icsac.2026.990",
                                                "source": "submission-pdf"})
    check(e.get("keywords") == ["alpha", "beta"], "a later upsert without keywords keeps them")
    reg.write_text("[]")

    # ── 3. the four writers pass them ────────────────────────────────────────
    print("3. writers")
    captured: list[dict] = []
    real_upsert = pub.upsert_entry
    pub.upsert_entry = lambda proto, **kw: (captured.append(proto), {})[1]
    try:
        import action
        proto = action._extract_registry_entry("12345", {"metadata": {
            "title": "T", "creators": [{"name": "Author, Test"}], "doi": "10.5281/zenodo.12345",
            "keywords": ["alpha", "beta."]}})
        check(proto.get("keywords") == ["alpha", "beta"], "Zenodo community accept: proto carries the record's keywords")
        proto = action._extract_registry_entry("12345", {"metadata": {"title": "T", "creators": []}})
        check("keywords" not in proto, "Zenodo community accept: no keywords -> no field")

        sub_dir = tmp / "ICSAC-SUB-99990"; sub_dir.mkdir()
        (sub_dir / "state.json").write_text("{}")
        sub = {"title": "T", "abstract": "A", "keywords": ["alpha", "beta"],
               "creators": [{"name": "Author, Test"}], "form": {"name": "Test Author"}, "license": "cc-by-4.0"}

        import crossref_deposit
        captured.clear()
        crossref_deposit._push_publications(sub_dir, sub, "10.5555/icsac.2026.990", log=lambda *a: None)
        check(captured and captured[0].get("keywords") == ["alpha", "beta"], "Crossref-route register: proto carries keywords")

        import publish_watcher
        captured.clear()
        try:
            publish_watcher._register_published(sub_dir, dict(sub, keywords=[]),
                                                {"doi": "10.5281/zenodo.12345", "record_id": "12345",
                                                 "metadata": {"title": "T", "keywords": ["from", "record"]}})
        except Exception:
            pass   # held entry: the function may stop after the upsert; the proto is what is checked
        check(captured and captured[0].get("keywords") == ["from", "record"],
              "Zenodo-route publish: falls back to the deposit's keywords")

        from intake import submission_worker as sw
        captured.clear()
        sw._register_doi_accept("ICSAC-SUB-99990", sub_dir, dict(sub, doi="10.5555/icsac.2026.990"))
        check(captured and captured[0].get("keywords") == ["alpha", "beta"], "DOI-route accept: proto carries keywords")
    finally:
        pub.upsert_entry = real_upsert

    # ── 4. DOI route: the form field wins over the source record ─────────────
    print("4. DOI route form field")
    src = (ROOT / "intake" / "submission_worker.py").read_text()
    check('keywords = submission.get("keywords") or review_meta.get("keywords") or []' in src,
          "worker: author keywords first, source record second")
    srv = (ROOT / "intake" / "intake_server.py").read_text()
    check(srv.count('form.get("keywords") or ""') == 2, "server: both handlers read the optional DOI-route field")

    # ── 5. the backfill tool ─────────────────────────────────────────────────
    print("5. backfill tool")
    spec = importlib.util.spec_from_file_location("backfill", ROOT / "tools" / "backfill-keywords.py")
    bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)
    subs = tmp / "subs"; (subs / "ICSAC-SUB-99991").mkdir(parents=True)
    (subs / "ICSAC-SUB-99991" / "submission.json").write_text(json.dumps({"keywords": ["one", "two."]}))
    bf.SUBMISSIONS_ROOT = subs
    check(bf.from_submission("ICSAC-SUB-99991") == ["one", "two"], "reads and cleans a submission's keywords")
    check(bf.from_submission("ICSAC-SUB-99999") == [], "a missing submission gives none")
    reg.write_text(json.dumps([{"slug": "t", "sub_id": "ICSAC-SUB-99991", "title": "T", "authors": ["A"],
                                "doi": "10.5555/icsac.2026.991", "accepted_date": "2026-10-02",
                                "source": "submission-pdf"}]))
    os.environ.pop("ICSAC_PUBLISH_APPROVED", None)
    sys.argv = ["backfill", "--write"]
    check(bf.main() == 2 and "keywords" not in json.loads(reg.read_text())[0], "--write refuses without approval")
    sys.argv = ["backfill"]
    check(bf.main() == 0 and "keywords" not in json.loads(reg.read_text())[0], "dry run writes nothing")
    os.environ["ICSAC_PUBLISH_APPROVED"] = "1"
    sys.argv = ["backfill", "--write"]
    check(bf.main() == 0 and json.loads(reg.read_text())[0].get("keywords") == ["one", "two"],
          "--write with approval fills the entry")
    os.environ.pop("ICSAC_PUBLISH_APPROVED", None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(0 if not failures else 1)
