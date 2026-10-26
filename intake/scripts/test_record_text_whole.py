#!/usr/bin/env python3
"""Checks that a paper's public record carries its abstract whole and counts its
panel the way the Review Process page does (2026-10-26). Offline: no network, no
mail, no pings; every write lands in one temporary directory that is removed at
the end, pass or fail. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_record_text_whole.py

Covers: each of the four registry writers (Zenodo community accept, DOI-route
accept, Zenodo-route publish, Crossref-route register) passing an abstract longer
than 2,000 characters through whole; the public review's opening sentence for a
two-pass panel (all slots answered, one slot failed), a three-pass panel, a
single-pass record (its old sentence, unchanged), a slot count that does not
divide by the passes, and a front matter whose pass count cannot be read; the
public RQC naming its flag's cause truly (a specificity pattern shared across
reviews is not described as a concern outside the scholarly dimensions; an
injection-only flag still gets the generic note; a low scholarly score gets
neither note).
"""
from __future__ import annotations

import re
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


LONG = " ".join(f"Sentence {i} of a long abstract ends here." for i in range(80))


def review_md(passes: str | None, n_slots: int, n_passes: int, failed: frozenset = frozenset()) -> str:
    fm = ['---', 'title: "Review: A Test Paper"', 'doi: ""', "record_id: ICSAC-SUB-TEST-31A",
          "review_date: 2026-01-01T00:00:00Z", "recommendation: RECOMMEND", "disagreement: False"]
    if passes is not None:
        fm.append(f"passes: {passes}")
    out = fm + ["---", "", "## Aggregate Scores", "", "| Dimension | Mean | Scores |",
                "|-----------|------|--------|", "| Domain Fit | 4.0 | 4, 4 |", "",
                "## Individual Model Reviews", ""]
    k = 0
    for p in range(1, n_passes + 1):
        for s in range(n_slots):
            head = f"panel-{chr(97 + s)}" + (f" (Pass {p})" if n_passes > 1 else "")
            out.append(f"### {head}\n")
            if k in failed:
                out.append("**Error:** the slot did not answer\n")
            else:
                out.append("**Recommendation:** RECOMMEND\n\n**Summary:** The argument is sound.\n\n"
                           "- **Domain Fit** (4/5): In scope.\n")
            k += 1
    out += ["---", "", "footer", ""]
    return "\n".join(out)


with tempfile.TemporaryDirectory(prefix="record-text-") as tmp_name:
    tmp = Path(tmp_name)

    # ── 1. the four registry writers keep the abstract whole ────────────────
    print("1. abstract whole in the registry")
    check(len(LONG) > 2000, f"fixture abstract is longer than 2,000 characters ({len(LONG)})")
    import publications as pub
    pub.READY_DIR = str(tmp / "ready-to-publish")
    captured: list[dict] = []
    real_upsert = pub.upsert_entry
    pub.upsert_entry = lambda proto, **kw: (captured.append(proto), {})[1]
    try:
        import action
        proto = action._extract_registry_entry("12345", {"metadata": {
            "title": "T", "creators": [{"name": "Author, Test"}], "doi": "10.5281/zenodo.12345",
            "description": f"<p>{LONG}</p>"}})
        check(proto.get("abstract") == LONG, "Zenodo community accept: abstract whole")

        sub_dir = tmp / "ICSAC-SUB-99980"
        sub_dir.mkdir()
        (sub_dir / "state.json").write_text("{}")
        sub = {"title": "T", "abstract": LONG, "keywords": ["alpha"],
               "creators": [{"name": "Author, Test"}], "form": {"name": "Test Author"}, "license": "cc-by-4.0"}

        import crossref_deposit
        captured.clear()
        crossref_deposit._push_publications(sub_dir, sub, "10.5555/icsac.2026.980", log=lambda *a: None)
        check(bool(captured) and captured[0].get("abstract") == LONG, "Crossref-route register: abstract whole")

        import publish_watcher
        captured.clear()
        try:
            publish_watcher._register_published(sub_dir, sub, {"doi": "10.5281/zenodo.12345", "record_id": "12345",
                                                               "metadata": {"title": "T"}})
        except Exception:
            pass   # held entry: the function may stop after the upsert; the proto is what is checked
        check(bool(captured) and captured[0].get("abstract") == LONG, "Zenodo-route publish: abstract whole")

        from intake import submission_worker as sw
        captured.clear()
        sw._register_doi_accept("ICSAC-SUB-99980", sub_dir, dict(sub, doi="10.5555/icsac.2026.980"))
        check(bool(captured) and captured[0].get("abstract") == LONG, "DOI-route accept: abstract whole")
    finally:
        pub.upsert_entry = real_upsert

    for rel in ("action.py", "crossref_deposit.py", "publish_watcher.py", "intake/submission_worker.py"):
        src = (ROOT / rel).read_text()
        check(not re.search(r'"abstract":[^\n]*\[:\d+\]', src), f"{rel}: no slice on the registry abstract")

    # ── 2. the public review counts the panel as the site does ──────────────
    print("2. panel sentence")
    import redaction as R

    def sentence(md_text: str, name: str) -> str:
        p = tmp / f"ICSAC-SUB-TEST-31A_{name}.md"
        p.write_text(md_text)
        md = R.build_public_markdown(R.parse_review_file(str(p)))
        R.assert_clean(md)
        m = re.search(r"^This submission was evaluated[^\n]*?six dimensions\.", md, re.M)
        return m.group(0) if m else ""

    s = sentence(review_md("2", 5, 2), "two-pass")
    check(s == "This submission was evaluated in ten reviews from a panel of five independent advanced AI "
               "reviewers, each run twice, scoring six dimensions.", f"two passes, all answered: {s!r}")
    s = sentence(review_md("2", 5, 2, failed=frozenset({7})), "two-pass-failed")
    check(s == "This submission was evaluated in nine reviews from a panel of five independent advanced AI "
               "reviewers, each run twice, scoring six dimensions.", f"two passes, one failed slot: {s!r}")
    s = sentence(review_md("3", 2, 3), "three-pass")
    check(s == "This submission was evaluated in six reviews from a panel of two independent advanced AI "
               "reviewers, each run three times, scoring six dimensions.", f"three passes: {s!r}")
    s = sentence(review_md(None, 2, 1), "single-pass")
    check(s == "This submission was evaluated by a panel of 2 independent advanced AI reviewers scoring six "
               "dimensions.", f"single pass keeps its old sentence: {s!r}")
    s = sentence(review_md("2", 3, 1), "uneven")
    check(s == "This submission was evaluated by a panel of 3 independent advanced AI reviewers scoring six "
               "dimensions.", f"slots that do not divide by the passes fall back: {s!r}")
    s = sentence(review_md("two", 2, 1), "unreadable")
    check(s.startswith("This submission was evaluated by a panel of 2 "), f"unreadable pass count reads as 1: {s!r}")

    # ── 3. the public RQC names the flag's cause truly ─────────────────────
    print("3. RQC flag cause")

    def rqc(flag: bool, spec: str, inj: str, rubric: str = "4/5"):
        slot = lambda: {"reviewer": "x", "errored": False, "dimensions": [
            ("Rubric Adherence", rubric, "Rubric followed."),
            ("Internal Consistency", "4/5", "Scores match the text."),
            ("Specificity", spec, "Generic in places."),
            ("Tone", "5/5", "Neutral."),
            ("Evidence Use", "4/5", "Uses the evidence block."),
            (R.RQC_INJECTION_DIM_LABEL, inj, "No signal."),
        ]}
        parsed = R.ParsedRQC(record_id="ICSAC-SUB-TEST-31A", title="A Test Paper", doi="",
                             audit_date="2026-01-01T00:00:00Z", flag=flag, summary="",
                             overall_concerns=[], slots=[slot() for _ in range(5)])
        md = R.build_public_rqc_markdown(parsed)
        R.assert_rqc_clean(md)
        return md

    outside = "outside the scholarly dimensions"
    pattern = "specificity pattern shared by several reviews"
    md = rqc(True, "3/5", "5/5")
    check(outside not in md and pattern in md, "flag from a shared pattern, injection clean: names the pattern")
    md = rqc(True, "4/5", "4/5")
    check(outside in md and pattern not in md, "flag from the injection score only: generic note kept")
    md = rqc(True, "4/5", "5/5", rubric="2/5")
    check(outside not in md and pattern not in md, "flag from a low scholarly score: neither note")
    md = rqc(False, "4/5", "5/5")
    check(outside not in md and pattern not in md, "not flagged: neither note")

print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(0 if not failures else 1)
