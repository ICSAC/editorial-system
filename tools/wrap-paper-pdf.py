#!/usr/bin/env python3
"""Wrap an ICSAC-accepted paper with a 1-page ICSAC cover.

Reads paper metadata from icsacinstitute.org/src/data/accepted.json,
renders a Letter-trim cover via WeasyPrint, and prepends it to the
author's PDF via pypdf. Output is written alongside the source PDF
with an `-icsac.pdf` suffix.

The author's manuscript is NEVER modified — the cover is a new first
page only. The unwrapped PDF stays in place at <record_id>.pdf so
Scholar / Zenodo / archival tooling can still grab the bare manuscript.

Usage:
  wrap-paper-pdf.py --slug the-dynamic-existence-threshold
  wrap-paper-pdf.py --all
  wrap-paper-pdf.py --slug X --fetch   # download source from Zenodo if missing
"""
from __future__ import annotations
import argparse
import io
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

import weasyprint
import pypdf

SITE = Path("/home/orangepi/Desktop/icsac/icsacinstitute.org")
LOGO = SITE / "public" / "logo_trans_ICSAC_notext.png"
ACCEPTED = SITE / "src" / "data" / "accepted.json"
OUT_DIR = SITE / "public" / "papers"

# License default per project policy
DEFAULT_LICENSE = "CC-BY 4.0"


def _accepted_us(iso: str) -> str:
    """2026-04-19 → 04/19/2026 (US format used elsewhere on the site)."""
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def _authors_str(authors: list[str]) -> str:
    if not authors:
        return "Anonymous"
    if len(authors) <= 3:
        return ", ".join(authors)
    return ", ".join(authors[:2]) + f", et al. ({len(authors)} authors)"


def _load_papers() -> list[dict]:
    d = json.loads(ACCEPTED.read_text())
    return d if isinstance(d, list) else d.get("papers", [])


def _find(slug: str) -> dict:
    for p in _load_papers():
        if p.get("slug") == slug:
            return p
    raise SystemExit(f"slug not found in accepted.json: {slug}")


def _zenodo_fetch_pdf(record_id: str, dest: Path) -> None:
    """Download the primary PDF file from a Zenodo record."""
    api = f"https://zenodo.org/api/records/{record_id}"
    print(f"  fetching Zenodo metadata: {api}")
    with urllib.request.urlopen(api, timeout=30) as r:
        meta = json.loads(r.read())
    files = meta.get("files", [])
    pdfs = [f for f in files if f.get("key", "").lower().endswith(".pdf")]
    if not pdfs:
        raise SystemExit(f"no PDF file on Zenodo record {record_id}")
    # Pick the largest (most likely the manuscript over supplementary)
    pdf = max(pdfs, key=lambda f: f.get("size", 0))
    url = pdf["links"]["self"]
    print(f"  downloading: {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as out:
        out.write(r.read())
    print(f"  saved → {dest}  ({dest.stat().st_size} bytes)")


_COVER_CSS = """
@page { size: Letter; margin: 0.75in 0.85in 1.1in 0.85in; }

body {
  font-family: "EB Garamond", Georgia, "Times New Roman", serif;
  font-size: 11pt;
  line-height: 1.5;
  color: #0A1929;
  margin: 0;
}

.header {
  display: flex;
  align-items: center;
  gap: 14pt;
  padding-bottom: 12pt;
  border-bottom: 1pt solid #8B5E3C;
}

.header img {
  width: 44pt;
  height: 44pt;
}

.header-text {
  display: flex;
  flex-direction: column;
}

.header-org {
  font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 9.5pt;
  letter-spacing: 1.2pt;
  text-transform: uppercase;
  color: #8B5E3C;
  font-weight: 600;
  line-height: 1.3;
}

.header-tag {
  font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 8.5pt;
  color: #6E5C42;
  margin-top: 3pt;
  letter-spacing: 0.3pt;
}

.title-block {
  margin: 32pt 0 0;
}

.title {
  font-size: 20pt;
  font-weight: 600;
  line-height: 1.22;
  margin: 0 0 10pt;
  color: #0A1929;
}

.authors {
  font-size: 11.5pt;
  color: #4B5563;
  font-style: italic;
  margin: 0 0 26pt;
}

.meta {
  display: grid;
  grid-template-columns: 115pt 1fr;
  gap: 7pt 16pt;
  font-size: 10pt;
  margin-bottom: 24pt;
}

.meta dt {
  font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
  color: #8B5E3C;
  font-weight: 600;
  letter-spacing: 0.6pt;
  text-transform: uppercase;
  font-size: 8pt;
  margin: 0;
}

.meta dd {
  color: #1f1f1f;
  margin: 0;
  word-break: break-word;
}

.meta dd a {
  color: #1f1f1f;
  text-decoration: none;
}

.venue {
  padding: 12pt 14pt;
  background-color: rgba(139, 94, 60, 0.06);
  border-left: 2pt solid #8B5E3C;
  font-size: 10pt;
  line-height: 1.55;
  margin-bottom: 22pt;
}

.venue strong {
  color: #8B5E3C;
}

.record {
  font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 9pt;
  color: #4B5563;
  margin-bottom: 18pt;
}

.record-label {
  letter-spacing: 0.6pt;
  text-transform: uppercase;
  font-size: 8pt;
  color: #8B5E3C;
  font-weight: 600;
  display: block;
  margin-bottom: 3pt;
}

.record a {
  color: #8B5E3C;
  text-decoration: none;
  word-break: break-all;
}

.note {
  position: fixed;
  bottom: 0.5in;
  left: 0.85in;
  right: 0.85in;
  padding-top: 8pt;
  border-top: 0.5pt solid #ccc;
  font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 7.5pt;
  line-height: 1.45;
  color: #888;
}
"""


def _render_cover(paper: dict) -> bytes:
    rid = paper.get("record_id") or ""
    slug = paper["slug"]
    title = paper["title"]
    authors = paper.get("authors") or []
    doi = paper.get("doi") or ""
    accepted = paper.get("accepted_date") or ""
    sub_id = paper.get("sub_id") or "—"

    doi_url = f"https://doi.org/{doi.lstrip('doi:').strip()}" if doi else ""
    record_url = f"https://icsacinstitute.org/publications/{slug}"

    # Logo as file:// URI so WeasyPrint loads it without fetching
    logo_uri = f"file://{LOGO}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>{_COVER_CSS}</style>
</head><body>
  <div class="header">
    <img src="{logo_uri}" alt="" />
    <div class="header-text">
      <div class="header-org">Institute for Complexity Science and Advanced Computing</div>
      <div class="header-tag">Open editorial record &middot; No fees &middot; Author retains copyright</div>
    </div>
  </div>

  <div class="title-block">
    <div class="title">{_escape(title)}</div>
    <div class="authors">{_escape(_authors_str(authors))}</div>
  </div>

  <dl class="meta">
    <dt>DOI</dt><dd>{_escape(doi)}</dd>
    <dt>Accepted</dt><dd>{_escape(_accepted_us(accepted))}</dd>
    <dt>License</dt><dd>{DEFAULT_LICENSE}</dd>
    <dt>Submission ID</dt><dd>{_escape(sub_id)}</dd>
  </dl>

  <div class="venue">
    <strong>Peer-reviewed by ICSAC</strong> &mdash; the Institute's open editorial record. The full record &mdash; AI panel reviews, Review Quality Control audit, and curator verdict &mdash; is publicly available at the URL below.
  </div>

  <div class="record">
    <span class="record-label">Editorial record</span>
    <a href="{record_url}">{record_url}</a>
  </div>

  <div class="note">
    This cover page is added by the Institute for Complexity Science and Advanced Computing. It does not modify the author's manuscript that follows. The author retains copyright under the license indicated; ICSAC publishes the editorial record but does not claim ownership of the work.
  </div>
</body></html>"""
    return weasyprint.HTML(string=html).write_pdf()


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap_one(paper: dict, fetch: bool = False) -> Path:
    rid = paper.get("record_id")
    slug = paper["slug"]
    if not rid:
        raise SystemExit(f"paper has no record_id (upload route not yet supported): {slug}")

    source = OUT_DIR / f"{rid}.pdf"
    if not source.exists():
        if fetch:
            print(f"  source PDF missing, fetching from Zenodo: {source}")
            _zenodo_fetch_pdf(rid, source)
        else:
            raise SystemExit(f"source PDF missing: {source}  (use --fetch to download)")

    print(f"  rendering cover for {slug}…")
    cover_bytes = _render_cover(paper)

    out = OUT_DIR / f"{rid}-icsac.pdf"
    writer = pypdf.PdfWriter()
    writer.append(fileobj=io.BytesIO(cover_bytes))
    writer.append(fileobj=str(source))
    with open(out, "wb") as f:
        writer.write(f)
    print(f"  wrote → {out}  ({out.stat().st_size} bytes)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--fetch", action="store_true",
                    help="Download source PDF from Zenodo if missing")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.all:
        papers = _load_papers()
        for p in papers:
            if not p.get("record_id"):
                print(f"SKIP {p['slug']} (no record_id)")
                continue
            print(f"\n→ {p['slug']}")
            wrap_one(p, fetch=args.fetch)
    elif args.slug:
        print(f"→ {args.slug}")
        wrap_one(_find(args.slug), fetch=args.fetch)
    else:
        ap.error("specify --slug or --all")


if __name__ == "__main__":
    main()
