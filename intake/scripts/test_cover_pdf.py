#!/usr/bin/env python3
"""Checks for the ICSAC cover edition of a published PDF (2026-10-18): a paper
without a Zenodo record_id gets wrapped (<sub_id>.pdf -> <sub_id>-icsac.pdf),
the cover names the journal, volume, article number, dates, licence and the
page's citation, the author's pages and the bare file are left untouched,
register --live makes the cover in the same push, and a failed cover is
reported (log, state.json, ping) without blocking the page.
Offline: no network, no mail, no pings; every write lands in one temporary
directory that is removed at the end, pass or fail. Run from the repo root
with the venv:
    .venv/bin/python intake/scripts/test_cover_pdf.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TOOL = ROOT / "tools" / "wrap-paper-pdf.py"

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def squeeze(text: str) -> str:
    """Text extraction splits words at kerned pairs ("T est"): compare without spaces."""
    return re.sub(r"\s+", "", text or "")


PERSISTENCE_TS = """export const PERSISTENCE_TITLE = 'Persistence';
export const PERSISTENCE_VOLUMES: ReadonlyArray<{ volume: string; from: string; year: string }> = [
  { volume: '1', from: '2026-01-01', year: '2026' },
];
export function isInstituteDoi(doi: string): boolean {
  return /^10\\.5555\\//i.test(bare(doi));
}
"""
CC_BY = "https://creativecommons.org/licenses/by/4.0/"
DEPOSIT_XML = """<?xml version="1.0"?>
<doi_batch xmlns="http://www.crossref.org/schema/5.4.0"><body><journal>
<journal_issue><publication_date media_type="online"><year>2026</year></publication_date></journal_issue>
<journal_article><publication_date media_type="online"><month>03</month><day>03</day><year>2026</year></publication_date>
<acceptance_date><month>03</month><day>02</day><year>2026</year></acceptance_date></journal_article>
</journal></body></doi_batch>"""

tmp = Path(tempfile.mkdtemp(prefix="cover-pdf-"))
try:
    import weasyprint
    import pypdf
    from PIL import Image

    def make_site(name: str) -> Path:
        site = tmp / name
        (site / "src" / "data").mkdir(parents=True)
        (site / "src" / "lib").mkdir(parents=True)
        (site / "public" / "papers").mkdir(parents=True)
        (site / "src" / "lib" / "persistence.ts").write_text(PERSISTENCE_TS)
        Image.new("RGBA", (330, 200), (139, 94, 60, 255)).save(site / "public" / "logo_trans_ICSAC_notext.png")
        return site

    def make_pdf(path: Path, pages: int = 2) -> None:
        body = "".join(f'<p style="page-break-after: always">Manuscript page {i + 1}. '
                       f'<a href="https://example.com/{i}">a link</a></p>' for i in range(pages))
        path.write_bytes(weasyprint.HTML(string=f"<html><body>{body}</body></html>").write_pdf())

    def run_tool(site: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                              env=dict(os.environ, ICSAC_WEBSITE_REPO=str(site)), timeout=300)

    PAPER = {"slug": "a-test-article", "sub_id": "ICSAC-SUB-09999", "title": "A Test Article",
             "authors": ["Ada B. Lovelace", "Alan Turing", "Grace Hopper"], "doi": "10.5555/icsac.2026.999",
             "accepted_date": "2026-03-03", "source": "submission-pdf", "license_url": CC_BY}

    # ── 1. a paper without a record_id gets wrapped ──────────────────────────
    print("1. a paper without a Zenodo record_id")
    site = make_site("site1")
    (site / "src" / "data" / "accepted.json").write_text(json.dumps([PAPER]))
    bare = site / "public" / "papers" / "ICSAC-SUB-09999.pdf"
    make_pdf(bare)
    before = sha(bare)
    r = run_tool(site, "--slug", "a-test-article", "--accepted", "2026-03-02", "--published", "2026-03-03")
    out = site / "public" / "papers" / "ICSAC-SUB-09999-icsac.pdf"
    check(r.returncode == 0 and out.exists(), "exit 0 and <sub_id>-icsac.pdf written beside the bare file")
    check(sha(bare) == before, "the bare file is byte-for-byte unchanged")
    got, src = pypdf.PdfReader(str(out)), pypdf.PdfReader(str(bare))
    check(len(got.pages) == len(src.pages) + 1, "one page added, no more")
    p1 = flat(got.pages[0].extract_text())
    for want, what in (("Persistence", "names the journal"),
                       ("VOLUME 1 (2026)", "names the volume and year"),
                       ("ARTICLE 999", "names the article number from the Institute DOI"),
                       ("10.5555/icsac.2026.999", "carries the DOI"),
                       ("03/02/2026", "carries the acceptance date"),
                       ("Published online", "labels the publication date"),
                       ("03/03/2026", "carries the publication date"),
                       ("CC BY 4.0", "carries the licence"),
                       ("https://icsacinstitute.org/publications/a-test-article", "carries the editorial record URL"),
                       ("Reviewed by the ICSAC panel", "keeps the open-review line"),
                       ("does not modify the author's manuscript", "keeps the does-not-modify paragraph")):
        check(want.lower() in p1.lower(), f"cover {what}")   # labels print in small caps (CSS uppercase)
    check("peer review" not in p1.lower() and "peer-review" not in p1.lower(), "cover never says peer review")
    check(squeeze("Lovelace, A. B., Turing, A., & Hopper, G. (2026). A Test Article. Persistence, 1, Article 999. "
                  "https://doi.org/10.5555/icsac.2026.999") in squeeze(p1), "cite-as line in the page's journal format")
    same = all(got.pages[i + 1].get_contents().get_data() == src.pages[i].get_contents().get_data()
               for i in range(len(src.pages)))
    check(same, "pages 2..n carry the author's content streams unchanged")
    check(all(len(got.pages[i + 1].get("/Annots") or []) == len(src.pages[i].get("/Annots") or [])
              for i in range(len(src.pages))), "the author's link annotations are kept")
    hv = lambda rd: tuple(map(int, re.match(r"%PDF-(\d+)\.(\d+)", rd.pdf_header).groups()))
    check(hv(got) >= hv(src), f"PDF header {got.pdf_header} is not older than the source's {src.pdf_header}")
    imgs = [(x.image.width, x.image.height) for x in got.pages[0].images]
    check(imgs and all(w <= 400 and h <= 250 for w, h in imgs), f"the cover carries only the small logo {imgs}")

    # ── 2. a Zenodo-route paper: first publication before acceptance ─────────
    print("2. a Zenodo-route paper")
    z = {"slug": "a-zenodo-paper", "record_id": "12345", "title": "A Zenodo Paper", "authors": ["Test Author"],
         "doi": "10.5281/zenodo.12345", "accepted_date": "2026-05-04", "source": "zenodo-community",
         "license_url": CC_BY}
    (site / "src" / "data" / "accepted.json").write_text(json.dumps([PAPER, z]))
    make_pdf(site / "public" / "papers" / "12345.pdf", pages=1)
    r = run_tool(site, "--slug", "a-zenodo-paper", "--published", "2026-02-02")
    p1 = flat(pypdf.PdfReader(str(site / "public" / "papers" / "12345-icsac.pdf")).pages[0].extract_text())
    check(r.returncode == 0, "exit 0")
    check("ARTICLE" not in p1.upper().split("VOLUME 1 (2026)")[1][:20], "no article number for a Zenodo DOI")
    check(p1.upper().find("FIRST PUBLISHED") < p1.upper().find("ACCEPTED") and "02/02/2026, on Zenodo" in p1,
          "dates in the order they happened, the first publication named as Zenodo's")
    check(squeeze("Author, T. (2026). A Zenodo Paper. Zenodo. https://doi.org/10.5281/zenodo.12345. "
                  "Reviewed by the ICSAC panel.") in squeeze(p1), "cite-as line in the page's Zenodo format")

    # ── 3. failures are reported, never silent, and write nothing ────────────
    print("3. failures")
    bad = dict(PAPER, slug="no-licence", sub_id="ICSAC-SUB-09998", license_url="https://example.com/mine")
    (site / "src" / "data" / "accepted.json").write_text(json.dumps([PAPER, z, bad]))
    make_pdf(site / "public" / "papers" / "ICSAC-SUB-09998.pdf", pages=1)
    b_before = sha(site / "public" / "papers" / "ICSAC-SUB-09998.pdf")
    r = run_tool(site, "--slug", "no-licence", "--accepted", "2026-03-02", "--published", "2026-03-03")
    check(r.returncode == 1 and "cover not made" in r.stderr and "licence" in r.stderr,
          "an unknown licence: exit 1, 'cover not made' with the reason")
    check(not (site / "public" / "papers" / "ICSAC-SUB-09998-icsac.pdf").exists(), "nothing written for it")
    check(sha(site / "public" / "papers" / "ICSAC-SUB-09998.pdf") == b_before, "its bare file untouched")
    missing = dict(PAPER, slug="no-pdf", sub_id="ICSAC-SUB-09997")
    (site / "src" / "data" / "accepted.json").write_text(json.dumps([missing]))
    r = run_tool(site, "--slug", "no-pdf", "--accepted", "2026-03-02", "--published", "2026-03-03")
    check(r.returncode == 1 and "source PDF missing" in r.stderr, "a missing source PDF: exit 1 with the reason")
    nokey = {k: v for k, v in PAPER.items() if k != "sub_id"} | {"slug": "doi-only"}
    (site / "src" / "data" / "accepted.json").write_text(json.dumps([missing, nokey]))
    r = run_tool(site, "--all")
    check(r.returncode == 1 and "nothing to cover" in r.stdout and "doi-only" in r.stdout,
          "--all names a paper with no PDF on the site instead of skipping it quietly")
    check("no-pdf" in r.stderr and "1 not made" in r.stdout, "--all reports the failure and counts it")

    # ── 4. the citation port matches the page's rules (cite.ts) ──────────────
    print("4. citation port")
    spec = importlib.util.spec_from_file_location("wrap_paper_pdf", TOOL)
    wp = importlib.util.module_from_spec(spec); spec.loader.exec_module(wp)
    vols = [{"volume": "1", "from": "2026-01-01", "year": "2026"}, {"volume": "2", "from": "2027-06-01", "year": "2027"}]
    check(wp.format_author_apa("Grace B. Hopper") == "Hopper, G. B.", "APA: initials, kept initial")
    check(wp.format_author_apa("Hopper, Grace") == "Hopper, Grace", "APA: 'Last, First' kept as written")
    check(wp.format_author_apa("Plato") == "Plato", "APA: a single name kept")
    check(wp.authors_apa_list([]) == "Anonymous", "no authors -> Anonymous")
    check(wp.authors_apa_list(["A B", "C D"]) == "B, A. & D, C.", "two authors joined with &")
    check(wp.volume_for("2027-07-01", vols)["volume"] == "2", "volume from the table by acceptance date")
    check(wp.article_number_for("doi:10.5555/icsac.2026.012", "10.5555") == "012", "article number from the DOI sequence")
    check(wp.article_number_for("10.5281/zenodo.1", "10.5555") is None, "no article number for a Zenodo DOI")
    real_ts = (Path(os.environ.get("ICSAC_WEBSITE_REPO", "")) / "src" / "lib" / "persistence.ts")
    if real_ts.exists():   # the operator checkout: the prefix the live page uses is read, not assumed
        check(re.fullmatch(r"10\.\d{4,9}", wp.Site(real_ts.parents[2]).journal()["prefix"] or "") is not None,
              "the website's persistence.ts yields a DOI prefix")
    site_j = wp.Site(site).journal()
    check(site_j["prefix"] == "10.5555" and site_j["title"] == "Persistence", "the fixture's prefix and title are read")

    # ── 5. register --live makes the cover in the same push ──────────────────
    print("5. register --live")
    import publications as pub
    import crossref_deposit as cd
    from intake import author_approval
    sys.modules["stats"] = types.SimpleNamespace(
        compute_stats=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stats stubbed")))
    pushed: list[list[str]] = []
    pings: list[str] = []
    pub.READY_DIR = str(tmp / "ready-to-publish")
    pub.stage_public_review_for_slug = lambda *a, **k: (None, None)
    pub.commit_and_push = lambda *a, **k: pushed.append(list(k.get("extra_paths") or []))
    cd.stage = lambda *a, **k: None
    cd.validate_xml = lambda b: None
    cd.deposit = lambda *a, **k: "batch-1"
    cd.poll_result = lambda *a, **k: {"status": "completed", "success": 1, "failure": 0}
    cd._ping = pings.append
    cd._publish_zenodo_archive = lambda *a, **k: None
    cd._notify_author = lambda *a, **k: True
    author_approval.gate = lambda *a, **k: (True, "approved (stub)")

    def make_sub(name: str, site: Path, with_pdf: bool = True) -> Path:
        sub = tmp / name
        (sub / "crossref").mkdir(parents=True)
        (sub / "submission.json").write_text(json.dumps({
            "sub_id": name, "title": f"Article {name}", "abstract": "A", "license": "cc-by-4.0",
            "creators": [{"name": "Lovelace, Ada"}], "form": {"name": "Ada Lovelace"}}))
        (sub / "state.json").write_text(json.dumps({"decision": "accept", "state": "completed"}))
        (sub / "crossref" / "deposit.xml").write_text(DEPOSIT_XML)
        (sub / "crossref" / "staged.json").write_text(json.dumps({"doi": "10.5555/icsac.2026.998", "registered": False}))
        if with_pdf:
            make_pdf(sub / "paper.pdf", pages=2)
        pub.WEBSITE_REPO = str(site)
        pub.REGISTRY_PATH = str(site / "src" / "data" / "accepted.json")
        return sub

    check(cd._deposited_dates(Path(tmp / "nope.xml")) == (None, None), "no deposit file -> no dates, no crash")
    site5 = make_site("site5"); (site5 / "src" / "data" / "accepted.json").write_text("[]")
    sub = make_sub("ICSAC-SUB-09990", site5)
    acc, pubd = cd._deposited_dates(sub / "crossref" / "deposit.xml")
    check((str(acc), str(pubd)) == ("2026-03-02", "2026-03-03"), "the deposited dates are read (the article's, not the issue's year)")
    cd.register(sub, live=True, log=lambda m: None)
    cov = site5 / "public" / "papers" / "ICSAC-SUB-09990-icsac.pdf"
    bare5 = site5 / "public" / "papers" / "ICSAC-SUB-09990.pdf"
    check(cov.exists(), "register --live wrote <sub_id>-icsac.pdf")
    check(sha(bare5) == sha(sub / "paper.pdf"), "the bare copy is the author's paper.pdf, untouched")
    check(pushed and str(cov) in pushed[-1] and str(bare5) in pushed[-1], "both files ride the same commit")
    st = json.loads((sub / "state.json").read_text())
    check(st.get("cover", {}).get("file") == "/papers/ICSAC-SUB-09990-icsac.pdf", "state.json records the cover")
    check(pings and "Cover page: made (/papers/ICSAC-SUB-09990-icsac.pdf)" in pings[-1], "the ping says the cover was made")
    p1 = flat(pypdf.PdfReader(str(cov)).pages[0].extract_text())
    check("03/02/2026" in p1 and "03/03/2026" in p1, "the cover carries the deposited dates")

    # ── 6. a failed cover is reported and does not block the page ────────────
    print("6. a failed cover")
    failing = tmp / "failing-tool.py"
    failing.write_text("import sys\nprint('cover not made: boom (stub)', file=sys.stderr)\nsys.exit(1)\n")
    real_tool = cd.COVER_TOOL
    cd.COVER_TOOL = failing
    try:
        site6 = make_site("site6"); (site6 / "src" / "data" / "accepted.json").write_text("[]")
        sub = make_sub("ICSAC-SUB-09991", site6)
        logs: list[str] = []
        out = cd.register(sub, live=True, log=logs.append)
        ck = json.loads((sub / "crossref" / "staged.json").read_text())["checkpoints"]
        st = json.loads((sub / "state.json").read_text())
        check(bool(ck.get("publications_pushed_at")) and out.get("author_draft") == "drafted",
              "the page is still published and the later steps run")
        check(st.get("cover", {}).get("error") == "boom (stub)", "state.json records why")
        check(any("COVER NOT MADE" in m and "boom" in m for m in logs), "the log says COVER NOT MADE with the reason")
        check("Cover page: NOT MADE (boom (stub))" in pings[-1] and "wrap-paper-pdf.py --slug" in pings[-1],
              "the ping says NOT MADE and how to make it")
        check(not (site6 / "public" / "papers" / "ICSAC-SUB-09991-icsac.pdf").exists()
              and all("icsac.pdf" not in p for p in pushed[-1]), "no cover file written or committed")
    finally:
        cd.COVER_TOOL = real_tool
    site7 = make_site("site7"); (site7 / "src" / "data" / "accepted.json").write_text("[]")
    sub = make_sub("ICSAC-SUB-09992", site7, with_pdf=False)
    cd.register(sub, live=True, log=lambda m: None)
    check("Cover page: NOT MADE (no paper.pdf" in pings[-1], "no paper.pdf: reported, not skipped")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
