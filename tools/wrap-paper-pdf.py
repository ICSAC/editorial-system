#!/usr/bin/env python3
"""Wrap an accepted Persistence article with a 1-page ICSAC cover.

Reads the article from the website's src/data/accepted.json and the volume
table from src/lib/persistence.ts, renders a cover the size of the author's
first page (Letter or A4) via WeasyPrint, and prepends it to the author's PDF
via pypdf. The author's PDF is public/papers/<key>.pdf and the result is
public/papers/<key>-icsac.pdf, where <key> is the Zenodo record_id for papers
deposited there and the submission ID (ICSAC-SUB-NNNNN) for papers registered
under the Institute's DOI prefix.

The author's manuscript is NEVER modified: the cover is a new first page only,
and the bare <key>.pdf stays in place for Scholar, the Crossref link and
archival tooling. After writing, the result is read back: page 1 must be the
cover and every later page must carry the source page's content unchanged.

Dates: --accepted / --published when given (register --live passes the dates
it deposited with Crossref). Otherwise an Institute DOI reads both from
Crossref, and a Zenodo DOI takes the acceptance date from the registry and the
first publication date from the Zenodo record. A date or licence that can't be
read stops the run: the cover never guesses.

Usage:
  wrap-paper-pdf.py --slug the-dynamic-existence-threshold
  wrap-paper-pdf.py --all
  wrap-paper-pdf.py --slug X --fetch    # download a missing Zenodo source PDF
  wrap-paper-pdf.py --slug X --accepted 2026-03-02 --published 2026-03-03
  wrap-paper-pdf.py --slug X --out DIR  # write the result to DIR instead

Exit 0 when every cover asked for was written; 1 when any was not ("cover not
made: <reason>" on stderr).
"""
from __future__ import annotations
import argparse
import base64
import datetime as _dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import weasyprint
import pypdf
from PIL import Image

SITE_URL = "https://icsacinstitute.org"
USER_AGENT = "ICSAC-cover-tool/2.0 (+https://icsacinstitute.org; mailto:info@icsacinstitute.org)"
PUBLISHER = "Institute for Complexity Science and Advanced Computing"

# Same labels as the article page (src/pages/publications/[slug].astro).
LICENSE_LABELS = {
    "https://creativecommons.org/licenses/by/4.0/": "CC BY 4.0",
    "https://creativecommons.org/licenses/by-sa/4.0/": "CC BY-SA 4.0",
    "https://creativecommons.org/publicdomain/zero/1.0/": "CC0 1.0",
}
# Zenodo licence ids -> the URL the registry would carry.
ZENODO_LICENSES = {
    "cc-by-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "cc-by-sa-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "cc0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
}
LOGO_HEIGHT_PT = 46
LOGO_PPI = 300


class CoverError(Exception):
    """This paper's cover can't be made; nothing is written for it."""


class Site:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.accepted = self.root / "src" / "data" / "accepted.json"
        self.persistence_ts = self.root / "src" / "lib" / "persistence.ts"
        self.papers = self.root / "public" / "papers"
        self.logo = self.root / "public" / "logo_trans_ICSAC_notext.png"
        self.fonts = self.root / "public" / "fonts"

    def load_papers(self) -> list[dict]:
        d = json.loads(self.accepted.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else d.get("papers", [])

    def find(self, slug: str) -> dict:
        for p in self.load_papers():
            if p.get("slug") == slug:
                return p
        raise CoverError(f"slug not found in accepted.json: {slug}")

    def journal(self) -> dict:
        """{title, volumes (oldest first), prefix} from persistence.ts, the file the
        article page reads: the journal title, the volume table, and the DOI
        prefix its isInstituteDoi() treats as the Institute's own."""
        ts = self.persistence_ts.read_text(encoding="utf-8")
        t = re.search(r"PERSISTENCE_TITLE\s*=\s*'([^']+)'", ts)
        m = re.search(r"PERSISTENCE_VOLUMES[^=]*=\s*\[(.*?)\];", ts, re.S)
        rows = re.findall(
            r"\{\s*volume:\s*'([^']+)',\s*from:\s*'(\d{4}-\d{2}-\d{2})',\s*year:\s*'(\d{4})'\s*,?\s*\}",
            m.group(1) if m else "")
        p = re.search(r"function isInstituteDoi\b.*?/\^10\\\.(\d{4,9})\\/", ts, re.S)
        if not t or not rows or not p:
            raise CoverError(f"couldn't read the journal title, volume table or DOI prefix from {self.persistence_ts}")
        vols = sorted(({"volume": v, "from": f, "year": y} for v, f, y in rows), key=lambda r: r["from"])
        return {"title": t.group(1), "volumes": vols, "prefix": f"10.{p.group(1)}"}


# ── the citation, ported from the site's src/lib/cite.ts + persistence.ts ──

def bare_doi(doi: str) -> str:
    return re.sub(r"^doi:", "", doi or "", flags=re.I).strip()


def is_institute_doi(doi: str, prefix: str) -> bool:
    return bare_doi(doi).lower().startswith(prefix.lower() + "/")


def article_number_for(doi: str, prefix: str) -> str | None:
    m = re.match(re.escape(prefix) + r"/icsac\.\d{4}\.(\d{3,})$", bare_doi(doi), re.I)
    return m.group(1) if m else None


def volume_for(accepted: str, volumes: list[dict]) -> dict | None:
    d = (accepted or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return None
    hit = None
    for row in volumes:
        if d >= row["from"]:
            hit = row
    return hit


def format_author_apa(name: str) -> str:
    t = name.strip()
    if not t:
        return ""
    if "," in t:
        return t
    parts = t.split()
    if len(parts) == 1:
        return parts[0]
    initials = [p if p.endswith(".") else p[0].upper() + "." for p in parts[:-1]]
    return f"{parts[-1]}, {' '.join(initials)}"


def authors_apa_list(authors: list[str]) -> str:
    f = [format_author_apa(a) for a in authors]
    if not f:
        return "Anonymous"
    if len(f) == 1:
        return f[0]
    if len(f) == 2:
        return f"{f[0]} & {f[1]}"
    return ", ".join(f[:-1]) + ", & " + f[-1]


def doi_url(doi: str) -> str:
    return "https://doi.org/" + re.sub(r"^doi:", "", doi, flags=re.I)


def published_on(paper: dict) -> str:
    """The publication date the site reads (published_date, else accepted_date):
    a citation's year and the volume follow publication, as in cite.ts."""
    return paper.get("published_date") or paper.get("accepted_date") or ""


def cite_as(paper: dict, j: dict) -> str:
    """The page's "The work" plain-text citation (cite.ts plainTextWork)."""
    authors = authors_apa_list(paper.get("authors") or [])
    year = published_on(paper)[:4]
    if is_institute_doi(paper["doi"], j["prefix"]):
        vol = volume_for(published_on(paper), j["volumes"])
        art = article_number_for(paper["doi"], j["prefix"])
        loc = j["title"] + (f", {vol['volume']}" if vol else "") + (f", Article {art}" if art else "")
        return f"{authors} ({year}). {paper['title']}. {loc}. {doi_url(paper['doi'])}"
    return f"{authors} ({year}). {paper['title']}. Zenodo. {doi_url(paper['doi'])}. Reviewed by the ICSAC panel."


# ── record lookups (dates, licence) ──────────────────────────────────────────

def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _iso(value: str, what: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat((value or "")[:10])
    except ValueError:
        raise CoverError(f"{what} isn't a date: {value!r}")


def _crossref_date(msg: dict, *keys: str) -> _dt.date | None:
    for k in keys:
        parts = ((msg.get(k) or {}).get("date-parts") or [[]])[0]
        if len(parts) == 3:
            return _dt.date(*parts)
    return None


def resolve_record(paper: dict, prefix: str, accepted: str | None, published: str | None) -> dict:
    """{accepted, published, published_where, license_url}, each from its record."""
    doi = bare_doi(paper["doi"])
    out = {"accepted": _iso(accepted, "--accepted") if accepted else None,
           "published": _iso(published, "--published") if published else None,
           "published_where": None,
           "license_url": paper.get("license_url") or None}
    if is_institute_doi(doi, prefix):
        if not (out["accepted"] and out["published"]):
            try:
                msg = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/"))["message"]
            except Exception as exc:
                raise CoverError(f"couldn't read the dates from Crossref for {doi} ({type(exc).__name__}: "
                                 f"{str(exc)[:80]}); pass --accepted and --published")
            out["accepted"] = out["accepted"] or _crossref_date(msg, "accepted")
            out["published"] = out["published"] or _crossref_date(msg, "published-online", "published")
            if not out["license_url"]:
                lic = [x.get("URL") for x in msg.get("license") or [] if x.get("content-version") == "vor"]
                out["license_url"] = lic[0] if lic else None
    else:
        out["accepted"] = out["accepted"] or _iso(paper.get("accepted_date"), "accepted_date")
        out["published_where"] = "Zenodo" if paper.get("record_id") else None
        if paper.get("record_id") and not (out["published"] and out["license_url"]):
            try:
                meta = _get_json(f"https://zenodo.org/api/records/{paper['record_id']}")["metadata"]
            except Exception as exc:
                raise CoverError(f"couldn't read Zenodo record {paper['record_id']} ({type(exc).__name__}: "
                                 f"{str(exc)[:80]}); pass --published")
            if not out["published"] and meta.get("publication_date"):
                out["published"] = _iso(meta["publication_date"], "Zenodo publication_date")
            if not out["license_url"]:
                out["license_url"] = ZENODO_LICENSES.get(((meta.get("license") or {}).get("id") or "").lower())
    if not out["accepted"]:
        raise CoverError(f"no acceptance date on record for {doi}; pass --accepted")
    if not out["published"]:
        raise CoverError(f"no publication date on record for {doi}; pass --published")
    if out["license_url"] not in LICENSE_LABELS:
        raise CoverError(f"licence for {doi} is {out['license_url'] or 'not on record'}; the cover names "
                         f"only {', '.join(LICENSE_LABELS.values())}")
    return out


# ── rendering ────────────────────────────────────────────────────────────────

def _us(d: _dt.date) -> str:
    return d.strftime("%m/%d/%Y")


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _logo_data_uri(path: Path) -> str:
    """The mark cropped to its ink and scaled to LOGO_PPI at the printed size, so
    the cover doesn't carry (and viewers don't decode) the 2568 px master."""
    im = Image.open(path).convert("RGBA")
    im = im.crop(im.getbbox() or (0, 0, *im.size))
    h = round(LOGO_HEIGHT_PT / 72 * LOGO_PPI)
    im = im.resize((round(im.width * h / im.height), h), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _font_faces(fonts: Path) -> str:
    faces = [("EB Garamond", "normal", "400 700", "eb-garamond-latin.woff2"),
             ("EB Garamond", "italic", "400", "eb-garamond-italic-latin.woff2"),
             ("Inter", "normal", "400 700", "inter-latin.woff2")]
    return "".join(
        f"@font-face {{ font-family: '{fam}'; font-style: {style}; font-weight: {weight}; "
        f"src: url('{(fonts / f).as_uri()}') format('woff2'); }}\n"
        for fam, style, weight, f in faces if (fonts / f).exists())


_COVER_CSS = """
@page { margin: 0.75in 0.85in 1.25in 0.85in; }
body { font-family: "EB Garamond", Georgia, "Times New Roman", serif; font-size: 11pt;
       line-height: 1.5; color: #0A1929; margin: 0; }
.sans { font-family: "Inter", "Helvetica Neue", Arial, sans-serif; }
.mast { display: flex; align-items: center; gap: 14pt; padding-bottom: 12pt;
        border-bottom: 1pt solid #8B5E3C; }
.mast img { height: 46pt; width: auto; }
.journal { font-size: 25pt; font-weight: 600; line-height: 1.05; color: #0A1929; letter-spacing: 0.2pt; }
.issue { font-family: "Inter", "Helvetica Neue", Arial, sans-serif; font-size: 9pt; font-weight: 600;
         letter-spacing: 1.1pt; text-transform: uppercase; color: #8B5E3C; margin-top: 5pt; }
.org { font-family: "Inter", "Helvetica Neue", Arial, sans-serif; font-size: 8.5pt; color: #6E5C42;
       margin-top: 2pt; letter-spacing: 0.2pt; }
.title { font-size: 20pt; font-weight: 600; line-height: 1.22; margin: 30pt 0 10pt; color: #0A1929; }
.authors { font-size: 11.5pt; color: #4B5563; font-style: italic; margin: 0 0 24pt; }
.meta { display: grid; grid-template-columns: 115pt 1fr; gap: 7pt 16pt; font-size: 10.5pt; margin: 0 0 22pt; }
.meta dt { font-family: "Inter", "Helvetica Neue", Arial, sans-serif; color: #8B5E3C; font-weight: 600;
           letter-spacing: 0.6pt; text-transform: uppercase; font-size: 8pt; margin: 0; padding-top: 2pt; }
.meta dd { color: #1f1f1f; margin: 0; overflow-wrap: anywhere; }
a { color: inherit; text-decoration: none; }
.venue { padding: 11pt 14pt; background-color: rgba(139, 94, 60, 0.06); border-left: 2pt solid #8B5E3C;
         font-size: 10.5pt; line-height: 1.55; margin-bottom: 20pt; }
.venue strong { color: #8B5E3C; }
.block { margin-bottom: 16pt; }
.label { font-family: "Inter", "Helvetica Neue", Arial, sans-serif; letter-spacing: 0.6pt;
         text-transform: uppercase; font-size: 8pt; color: #8B5E3C; font-weight: 600; display: block;
         margin-bottom: 3pt; }
.record a { font-family: "Inter", "Helvetica Neue", Arial, sans-serif; font-size: 9pt; color: #8B5E3C;
            overflow-wrap: anywhere; }
.cite p { margin: 0; font-size: 10.5pt; line-height: 1.5; overflow-wrap: anywhere; }
.note { position: fixed; bottom: -0.65in; left: 0; right: 0; padding-top: 8pt; border-top: 0.5pt solid #ccc;
        font-family: "Inter", "Helvetica Neue", Arial, sans-serif; font-size: 7.5pt; line-height: 1.45;
        color: #6b6b6b; }
"""


def _page_size(page) -> str:
    """The author's first page size, so the file has one page width (A4 papers
    get an A4 cover); Letter when that page is landscape or an odd size."""
    w, h = float(page.mediabox.width), float(page.mediabox.height)
    if (page.get("/Rotate") or 0) % 180:
        w, h = h, w
    return f"{w:.2f}pt {h:.2f}pt" if 500 <= w <= 700 and 700 <= h <= 900 else "Letter"


def render_cover(paper: dict, rec: dict, site: Site, size: str = "Letter") -> bytes:
    j = site.journal()
    doi = bare_doi(paper["doi"])
    vol = volume_for(published_on(paper), j["volumes"])
    art = article_number_for(doi, j["prefix"])
    issue = " &middot; ".join(x for x in (
        f"Volume {_escape(vol['volume'])} ({_escape(vol['year'])})" if vol else "",
        f"Article {_escape(art)}" if art else "") if x)
    record_url = f"{SITE_URL}/publications/{paper['slug']}"
    if rec["published_where"]:
        pub_dt, pub_dd = "First published", f"{_us(rec['published'])}, on {_escape(rec['published_where'])}"
    else:
        pub_dt, pub_dd = "Published online", _us(rec["published"])
    # The two dates in the order they happened: a Zenodo deposit predates its acceptance.
    dates = [("Accepted", _us(rec["accepted"])), (pub_dt, pub_dd)]
    if rec["published"] < rec["accepted"]:
        dates.reverse()
    authors = paper.get("authors") or []
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_escape(paper['title'])}</title>
<style>{_font_faces(site.fonts)}{_COVER_CSS}@page {{ size: {size}; }}</style>
</head><body>
  <div class="mast">
    <img src="{_logo_data_uri(site.logo)}" alt="" />
    <div>
      <div class="journal">{_escape(j['title'])}</div>
      {f'<div class="issue">{issue}</div>' if issue else ''}
      <div class="org">{PUBLISHER}</div>
    </div>
  </div>

  <div class="title">{_escape(paper['title'])}</div>
  <div class="authors">{_escape(', '.join(authors) or 'Anonymous')}</div>

  <dl class="meta">
    <dt>DOI</dt><dd><a href="{_escape(doi_url(doi))}">{_escape(doi)}</a></dd>
{''.join(f'    <dt>{t}</dt><dd>{d}</dd>' + chr(10) for t, d in dates)}    <dt>License</dt><dd><a href="{_escape(rec['license_url'])}">{LICENSE_LABELS[rec['license_url']]}</a></dd>
  </dl>

  <div class="venue">
    <strong>Reviewed by the ICSAC panel.</strong> The full editorial record, with the AI panel reviews, the Review Quality Control audit and the curation team&rsquo;s verdict, is public at the URL below.
  </div>

  <div class="block record">
    <span class="label">Editorial record</span>
    <a href="{record_url}">{record_url}</a>
  </div>

  <div class="block cite">
    <span class="label">Cite as</span>
    <p>{_escape(cite_as(paper, j))}</p>
  </div>

  <div class="note">
    This cover page is added by the Institute for Complexity Science and Advanced Computing. It does not modify the author's manuscript that follows. The author retains copyright under the license indicated; ICSAC publishes the editorial record but does not claim ownership of the work.
  </div>
</body></html>"""
    return weasyprint.HTML(string=html).write_pdf()


# ── wrapping ─────────────────────────────────────────────────────────────────

def _zenodo_fetch_pdf(record_id: str, dest: Path) -> None:
    """Download the primary PDF file from a Zenodo record."""
    files = _get_json(f"https://zenodo.org/api/records/{record_id}").get("files", [])
    pdfs = [f for f in files if f.get("key", "").lower().endswith(".pdf")]
    if not pdfs:
        raise CoverError(f"no PDF file on Zenodo record {record_id}")
    pdf = max(pdfs, key=lambda f: f.get("size", 0))   # the manuscript over a supplement
    req = urllib.request.Request(pdf["links"]["self"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        _write_atomic(dest, r.read())
    print(f"  saved {dest} ({dest.stat().st_size} bytes)")


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _header_version(reader: pypdf.PdfReader) -> tuple[int, int]:
    m = re.match(r"%PDF-(\d+)\.(\d+)", reader.pdf_header or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1, 3)


def _content_digest(page) -> str:
    c = page.get_contents()
    return hashlib.sha256(c.get_data() if c is not None else b"").hexdigest()


def pdf_key(paper: dict) -> str | None:
    return paper.get("record_id") or paper.get("sub_id") or None


def wrap_one(paper: dict, site: Site, *, fetch: bool = False, accepted: str | None = None,
             published: str | None = None, out_dir: Path | None = None) -> Path:
    slug = paper["slug"]
    key = pdf_key(paper)
    if not key:
        raise CoverError(f"{slug} has neither a Zenodo record_id nor a submission ID, so no PDF of it is on the site")
    source = site.papers / f"{key}.pdf"
    if not source.exists():
        if fetch and paper.get("record_id"):
            print(f"  source PDF missing, fetching from Zenodo: {source}")
            _zenodo_fetch_pdf(paper["record_id"], source)
        else:
            raise CoverError(f"source PDF missing: {source}"
                             + ("  (use --fetch to download)" if paper.get("record_id") else ""))
    src_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    src = pypdf.PdfReader(str(source))
    if src.is_encrypted:
        raise CoverError(f"{source.name} is encrypted; the cover tool won't rewrite it")

    rec = resolve_record(paper, site.journal()["prefix"], accepted, published)
    print(f"  rendering cover for {slug} (accepted {rec['accepted']}, published {rec['published']}"
          f"{', on ' + rec['published_where'] if rec['published_where'] else ''}, {LICENSE_LABELS[rec['license_url']]})")
    cover = pypdf.PdfReader(io.BytesIO(render_cover(paper, rec, site, _page_size(src.pages[0]))))
    if len(cover.pages) != 1:
        raise CoverError(f"the cover for {slug} rendered to {len(cover.pages)} pages, not 1")

    writer = pypdf.PdfWriter()
    writer.append(cover)
    writer.append(src)
    major, minor = max(_header_version(src), _header_version(cover))
    writer.pdf_header = f"%PDF-{major}.{minor}"
    writer.add_metadata({"/Title": paper["title"], "/Author": ", ".join(paper.get("authors") or [])})
    buf = io.BytesIO()
    writer.write(buf)

    # Read the result back before it replaces anything: the cover first, then the
    # author's pages with their content streams unchanged.
    got = pypdf.PdfReader(io.BytesIO(buf.getvalue()))
    if len(got.pages) != len(src.pages) + 1:
        raise CoverError(f"the result has {len(got.pages)} pages, expected {len(src.pages) + 1}")
    if bare_doi(paper["doi"]) not in re.sub(r"\s+", "", got.pages[0].extract_text() or ""):
        raise CoverError("page 1 of the result doesn't read as the cover (its DOI isn't on it)")
    for i, page in enumerate(src.pages):
        if _content_digest(got.pages[i + 1]) != _content_digest(page):
            raise CoverError(f"page {i + 2} of the result doesn't match page {i + 1} of {source.name}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != src_sha:
        raise CoverError(f"{source.name} changed while its cover was being made")

    out = (Path(out_dir) if out_dir else site.papers) / f"{key}-icsac.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(out, buf.getvalue())
    print(f"  wrote {out} ({out.stat().st_size} bytes, {len(got.pages)} pages, {writer.pdf_header})")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--fetch", action="store_true", help="Download a missing source PDF from Zenodo")
    ap.add_argument("--accepted", help="acceptance date, YYYY-MM-DD (--slug only)")
    ap.add_argument("--published", help="publication date, YYYY-MM-DD (--slug only)")
    ap.add_argument("--out", type=Path, help="write <key>-icsac.pdf here instead of public/papers")
    args = ap.parse_args(argv)
    site_root = os.environ.get("ICSAC_WEBSITE_REPO")
    if not site_root:
        ap.error("set ICSAC_WEBSITE_REPO to the website checkout (the folder holding src/data/accepted.json)")
    if not (args.slug or args.all) or (args.slug and args.all):
        ap.error("specify --slug or --all")
    if args.all and (args.accepted or args.published):
        ap.error("--accepted and --published go with --slug")
    site = Site(Path(site_root))

    failed = 0
    if args.all:
        papers = site.load_papers()
    else:
        try:
            papers = [site.find(args.slug)]
        except CoverError as exc:
            print(f"cover not made: {exc}", file=sys.stderr)
            return 1
    for p in papers:
        print(f"-> {p['slug']}")
        if args.all and not pdf_key(p):
            print(f"  no PDF on the site for {p['slug']} (no record_id or sub_id): nothing to cover")
            continue
        try:
            wrap_one(p, site, fetch=args.fetch, accepted=args.accepted, published=args.published,
                     out_dir=args.out)
        except CoverError as exc:
            failed += 1
            print(f"cover not made: {p['slug']}: {exc}", file=sys.stderr)
    if args.all:
        print(f"{len(papers) - failed} of {len(papers)} handled, {failed} not made")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
