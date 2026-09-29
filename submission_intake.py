"""Fetch metadata and PDF from Zenodo by DOI."""

import json
import os
import re
import subprocess
import tempfile
import urllib.request
import urllib.error
import urllib.parse

import config


PDF_TEXT_MAX_CHARS = 150000
# Text shorter than this from pdftotext is treated as extraction failure
# (likely image-based PDF). Triggers OCR fallback.
PDF_TEXT_MIN_CHARS = 2000


def _pdftotext(pdf_path, max_chars):
    """Run pdftotext. Returns decoded text or empty string on failure."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-nopgbrk", pdf_path, "-"],
            capture_output=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if result.returncode != 0:
        return ""
    text = result.stdout.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... truncated ...]"
    return text


def _ocr_pdf(pdf_path, max_chars):
    """OCR fallback for image-based PDFs: pdftoppm rasterizes pages to
    grayscale PPM, tesseract OCRs each page, results are concatenated.

    Returns empty string if either tool is missing, pdftoppm produces no
    pages, or every page fails to OCR.
    """
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "page")
        try:
            r = subprocess.run(
                ["pdftoppm", "-r", "200", "-gray", pdf_path, prefix],
                capture_output=True,
                timeout=240,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        if r.returncode != 0:
            return ""
        pages = sorted(
            os.path.join(tmp, f) for f in os.listdir(tmp)
            if f.startswith("page-") and f.endswith((".ppm", ".pgm", ".pbm"))
        )
        if not pages:
            return ""
        parts = []
        total = 0
        for page in pages:
            try:
                t = subprocess.run(
                    ["tesseract", page, "-", "-l", "eng"],
                    capture_output=True,
                    timeout=90,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
            if t.returncode != 0:
                continue
            page_text = t.stdout.decode("utf-8", errors="replace")
            parts.append(page_text)
            total += len(page_text)
            if total >= max_chars:
                break
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[... truncated ...]"
        return text


def extract_pdf_text(pdf_path, max_chars=PDF_TEXT_MAX_CHARS):
    """Extract plain text from a PDF via pdftotext (poppler).

    If the PDF lacks a text layer (image-only scans, some Print-To-PDF
    chains), pdftotext returns little or nothing and the short result is
    returned as-is. Downstream pipeline code compares the length to
    PDF_TEXT_MIN_CHARS and refuses to review — ICSAC requires text-layer
    PDFs.

    OCR is deliberately NOT auto-invoked: tesseract output is reliable for
    prose but mangles equations, Greek letters, and citation DOIs, which
    are exactly the content Methodology and Citation Integrity dimensions
    depend on. `_ocr_pdf()` remains callable for manual operator use when
    deciding whether to override a rejection.
    """
    if not pdf_path or not os.path.isfile(pdf_path):
        return ""
    return _pdftotext(pdf_path, max_chars)


def doi_to_record_id(doi: str) -> str:
    """Extract Zenodo record ID from a DOI like 10.5281/zenodo.18182662."""
    match = re.search(r"zenodo\.(\d+)", doi)
    if match:
        return match.group(1)
    raise ValueError(f"Cannot extract Zenodo record ID from DOI: {doi}")


def fetch_metadata(record_id: str) -> dict:
    """Fetch record metadata from Zenodo REST API."""
    url = f"{config.ZENODO_API}/records/{record_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")

    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def extract_review_data(metadata: dict) -> dict:
    """Extract fields relevant for the reviewer panel from Zenodo metadata."""
    m = metadata.get("metadata", metadata)

    # Get description/abstract — strip HTML tags
    description = m.get("description", "")
    description = re.sub(r"<[^>]+>", "", description)

    creators = []
    for c in m.get("creators", []):
        name = c.get("name", c.get("person_or_org", {}).get("name", "Unknown"))
        creators.append(name)

    # Related identifiers (references/citations)
    related = m.get("related_identifiers", [])

    # Keywords
    keywords = m.get("keywords", [])
    if not keywords:
        subjects = m.get("subjects", [])
        keywords = [s.get("subject", s) if isinstance(s, dict) else s for s in subjects]

    return {
        "record_id": metadata.get("id", ""),
        "doi": m.get("doi", metadata.get("doi", "")),
        "title": m.get("title", "Untitled"),
        "creators": creators,
        "description": description,
        "keywords": keywords,
        "publication_date": m.get("publication_date", ""),
        "resource_type": m.get("resource_type", {}),
        "license": m.get("license", {}),
        "related_identifiers": related,
        "version": m.get("version", ""),
    }


def download_pdf(metadata: dict, dest_dir: str = None) -> str | None:
    """Download the first PDF file from a Zenodo record. Returns path or None."""
    dest_dir = dest_dir or config.DOWNLOADS_DIR
    os.makedirs(dest_dir, exist_ok=True)

    files = metadata.get("files", [])
    if not files:
        return None

    pdf_entry = None
    for f in files:
        key = f.get("key", f.get("filename", ""))
        if key.lower().endswith(".pdf"):
            pdf_entry = f
            break

    if not pdf_entry:
        return None

    # Build download URL
    key = os.path.basename(pdf_entry.get("key", pdf_entry.get("filename", "")))
    record_id = metadata.get("id", "")

    # Try links.self first, fall back to constructed URL
    link = pdf_entry.get("links", {}).get("self")
    if not link:
        link = f"{config.ZENODO_API}/records/{record_id}/files/{key}/content"

    dest_path = os.path.join(dest_dir, f"{record_id}_{key}")
    if os.path.exists(dest_path):
        return dest_path

    req = urllib.request.Request(link)
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")

    # Same cap as an upload: a hosted "PDF" cannot fill the disk (audit 2026-09-28 pass B).
    max_bytes = int(os.environ.get("INTAKE_MAX_PDF_BYTES", str(100 * 1024 * 1024)))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = 0
            with open(dest_path, "wb") as out:
                while chunk := resp.read(8192):
                    total += len(chunk)
                    if total > max_bytes:
                        out.close()
                        os.remove(dest_path)
                        print(f"  Warning: PDF download exceeds {max_bytes // (1024 * 1024)} MB; refused")
                        return None
                    out.write(chunk)
        return dest_path
    except urllib.error.URLError as e:
        print(f"  Warning: PDF download failed: {e}")
        return None


def ingest_doi(doi: str) -> dict:
    """Full ingestion: fetch metadata, extract review data, download PDF."""
    record_id = doi_to_record_id(doi)
    print(f"  Fetching metadata for record {record_id}...")
    metadata = fetch_metadata(record_id)

    review_data = extract_review_data(metadata)

    print(f"  Downloading PDF...")
    pdf_path = download_pdf(metadata)
    review_data["pdf_path"] = pdf_path
    review_data["raw_metadata"] = metadata

    full_text = extract_pdf_text(pdf_path) if pdf_path else ""
    review_data["full_text"] = full_text
    if full_text:
        print(f"  Extracted {len(full_text)} chars of PDF text")
    elif pdf_path:
        print(f"  Warning: PDF text extraction failed — reviewers will see abstract only")

    return review_data


# ─── arXiv resolver ────────────────────────────────────────────────────────
# Used by the icsac-submission-intake project, not by the Zenodo watcher.
# Kept here in submission_intake.py so DOI-source resolution stays centralized — both
# the watcher path (Zenodo only) and the intake path (Zenodo OR arXiv) use
# this module as the single ingestion surface. Pipeline's other modules
# (review, redaction, etc.) accept any review_data dict matching the shape
# extract_review_data produces, regardless of source.

import xml.etree.ElementTree as _ET

_ARXIV_DOI_RE = re.compile(
    r"^10\.48550/arXiv\.(\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE
)
_ARXIV_BARE_ID_RE = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$")


def is_arxiv_ref(s: str) -> bool:
    """True if s looks like an arXiv DOI (10.48550/arXiv.X) or a bare
    modern-format arXiv ID (e.g. 2103.12345 or 2103.12345v1). False for
    pre-2007 IDs (math.GT/0309136 style) — those are out of scope here."""
    if not s:
        return False
    return bool(_ARXIV_DOI_RE.match(s) or _ARXIV_BARE_ID_RE.match(s))


def arxiv_ref_to_id(s: str) -> str:
    """Extract the bare arXiv ID. Strips any version suffix; arXiv's PDF
    URL always returns the latest version when no suffix is given, which
    matches our 'review what's currently posted' contract."""
    m = _ARXIV_DOI_RE.match(s)
    if m:
        return m.group(1)
    m = _ARXIV_BARE_ID_RE.match(s)
    if m:
        return m.group(1)
    raise ValueError(f"not an arXiv reference: {s!r}")


_CC_LICENSES = {
    "creativecommons.org/licenses/by/4.0": "cc-by-4.0",
    "creativecommons.org/licenses/by-sa/4.0": "cc-by-sa-4.0",
    "creativecommons.org/licenses/by-nc-sa/4.0": "cc-by-nc-sa-4.0",
    "creativecommons.org/licenses/by-nc-nd/4.0": "cc-by-nc-nd-4.0",
    "creativecommons.org/publicdomain/zero/1.0": "cc0-1.0",
    "arxiv.org/licenses/nonexclusive-distrib/1.0": "arxiv-nonexclusive-1.0",
}


_SPDX_LICENSES = {"cc-by-4.0", "cc-by-sa-4.0", "cc-by-nc-sa-4.0", "cc-by-nc-nd-4.0", "cc0-1.0"}


def _license_id_from_uri(uri: str) -> str:
    """A licence id for a rights URI. arXiv writes CC links with a /legalcode
    suffix (seen 2026-09-29); /deed.<lang> pages are stripped the same way."""
    u = re.sub(r"^https?://(www\.)?", "", (uri or "").strip().lower()).rstrip("/")
    u = re.sub(r"/(legalcode|deed)(\.[a-z-]+)?$", "", u)
    return _CC_LICENSES.get(u, "")


def _license_id(right: dict) -> str:
    """DataCite's SPDX rightsIdentifier when it is one we know, else the URI."""
    spdx = (right.get("rightsIdentifier") or "").strip().lower()
    if spdx in _SPDX_LICENSES:
        return spdx
    return _license_id_from_uri(right.get("rightsUri") or "")


def _arxiv_from_datacite(arxiv_id: str) -> dict | None:
    """arXiv registers every e-print with DataCite (10.48550/arXiv.<id>): title,
    authors, abstract, dates, subjects, the licence, and the journal DOI once
    published. Read from there, arXiv's own API is not touched. None when
    DataCite has no record."""
    url = f"https://api.datacite.org/dois/10.48550/arXiv.{urllib.parse.quote(arxiv_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": "ICSAC-pipeline/1.0 (mailto:help@icsacinstitute.org)",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            a = json.load(resp)["data"]["attributes"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    title = ((a.get("titles") or [{}])[0].get("title") or "").strip()
    if not title:
        return None
    creators = []
    for c in a.get("creators") or []:
        given, family = (c.get("givenName") or "").strip(), (c.get("familyName") or "").strip()
        name = f"{given} {family}".strip() if (given and family) else (c.get("name") or "").strip()
        if name:
            creators.append(name)
    abstract = next((d.get("description") or "" for d in a.get("descriptions") or []
                     if d.get("descriptionType") == "Abstract"), "")
    subjects = [s.get("subject") or "" for s in a.get("subjects") or []]
    m = re.search(r"\(([^()]+)\)\s*$", subjects[0]) if subjects else None
    keywords = [m.group(1)] if m else ([subjects[0]] if subjects else [])
    submitted = next((d.get("date") or "" for d in a.get("dates") or [] if d.get("dateType") == "Submitted"), "")
    lic = next((lid for lid in (_license_id(r) for r in a.get("rightsList") or []) if lid), "")
    related = [{"identifier": r.get("relatedIdentifier"), "relation": r.get("relationType")}
               for r in a.get("relatedIdentifiers") or [] if r.get("relatedIdentifier")]
    return {
        "record_id": arxiv_id,
        "doi": f"10.48550/arXiv.{arxiv_id}",
        "title": re.sub(r"\s+", " ", title),
        "creators": creators,
        "description": abstract.strip(),
        "keywords": keywords,
        "publication_date": submitted[:10] or str(a.get("publicationYear") or ""),
        "resource_type": {"type": "publication", "subtype": "preprint"},
        "license": {"id": lic},
        "related_identifiers": related,
        "version": "1",
        "metadata_source": "datacite",
    }


def fetch_arxiv_metadata(arxiv_id: str) -> dict:
    """arXiv metadata, shaped like extract_review_data() output so
    review.review_paper can use it without branching on source.

    DataCite first (2026-09-29): it carries everything the pipeline reads plus
    the licence, and it is not arXiv's rate-limited API. arXiv's Atom API is
    the fallback, and it goes through arxiv_gate (arXiv's limit: one request
    every three seconds, one connection, for all our machines together).
    """
    try:
        meta = _arxiv_from_datacite(arxiv_id)
        if meta:
            return meta
    except Exception as exc:
        import sys as _sys
        print(f"  arXiv metadata: DataCite lookup failed for {arxiv_id} ({type(exc).__name__}: {exc}); "
              f"trying arXiv's API", file=_sys.stderr)
    import arxiv_gate
    url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv_id)}"
    atom = arxiv_gate.get(url, timeout=30, attempts=3).decode("utf-8", errors="replace")

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = _ET.fromstring(atom)
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise ValueError(f"arXiv API returned no entry for {arxiv_id!r}")

    # arXiv often returns an error placeholder entry for unknown IDs;
    # detect it by missing <id> or <title> ending in "Error".
    entry_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
    title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
    if not entry_id or "arXiv.org Error" in title:
        raise ValueError(f"arXiv has no record for {arxiv_id!r}")

    summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
    published = (entry.findtext("atom:published", default="", namespaces=ns) or "")[:10]

    creators: list = []
    for author in entry.findall("atom:author", ns):
        name = author.findtext("atom:name", default="", namespaces=ns)
        if name:
            creators.append(name.strip())

    primary_category = entry.find("arxiv:primary_category", ns)
    category = primary_category.get("term", "") if primary_category is not None else ""
    keywords = [category] if category else []

    return {
        "record_id": arxiv_id,
        "doi": f"10.48550/arXiv.{arxiv_id}",
        "title": title,
        "creators": creators,
        "description": summary,
        "keywords": keywords,
        "publication_date": published,
        "resource_type": {"type": "publication", "subtype": "preprint"},
        "license": {"id": ""},
        "related_identifiers": [],
        "version": "1",
    }


def download_arxiv_pdf(arxiv_id: str, dest_dir: str = None) -> str | None:
    """Download an arXiv PDF. Returns local path or None on failure.

    No version suffix on the URL — arXiv returns the latest version,
    which is what the panel should review. If the operator wants a
    specific version, they'd submit the bare ID with version suffix and
    we'd need to extend arxiv_ref_to_id; not done here.
    """
    dest_dir = dest_dir or config.DOWNLOADS_DIR
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, f"{arxiv_id}.pdf")
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024:
        return dest_path

    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    req = urllib.request.Request(
        url, headers={"User-Agent": "ICSAC-pipeline/1.0"}
    )
    try:
        max_bytes = int(os.environ.get("INTAKE_MAX_PDF_BYTES", str(100 * 1024 * 1024)))
        total = 0
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(dest_path, "wb") as out:
                while chunk := resp.read(8192):
                    total += len(chunk)
                    if total > max_bytes:
                        # the Zenodo path has had this ceiling; arXiv had none (audit 2026-09-29)
                        out.close()
                        os.remove(dest_path)
                        print(f"  Warning: arXiv PDF exceeds {max_bytes // (1024 * 1024)} MB; refused")
                        return None
                    out.write(chunk)
        # arXiv occasionally returns an HTML "paper not yet available" stub
        # at the PDF URL; reject anything not starting with %PDF-.
        with open(dest_path, "rb") as f:
            head = f.read(5)
        if not head.startswith(b"%PDF-"):
            os.remove(dest_path)
            return None
        return dest_path
    except urllib.error.URLError as e:
        print(f"  arXiv PDF download failed: {e}")
        return None



# ─── Crossref + Semantic Scholar resolvers ──────────────────────────────
# Used by citation_verify.py. Both endpoints are key-less and free; UA
# strings carry the institutional contact email per the providers'
# polite-pool conventions.

CITATION_HTTP_TIMEOUT = 15


def fetch_crossref_metadata(doi: str) -> dict | None:
    """Crossref REST: GET https://api.crossref.org/works/<doi>.

    Returns a flat dict {title, authors, abstract, year, type, doi} or
    None on 404 / parse error / network error. Crossref's "abstract"
    field is JATS-tagged XML when present — strip tags before returning.
    """
    if not doi:
        return None
    safe = urllib.parse.quote(doi, safe="/")
    url = f"https://api.crossref.org/works/{safe}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ICSAC-pipeline/1.0 (mailto:info@icsacinstitute.org)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CITATION_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    msg = data.get("message") or {}
    title_list = msg.get("title") or []
    title = title_list[0].strip() if title_list else ""
    if not title:
        return None
    abstract = msg.get("abstract") or ""
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()
    authors = []
    for a in msg.get("author", []) or []:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        full = (f"{given} {family}").strip() or family or given
        if full:
            authors.append(full)
    year = None
    issued = msg.get("issued") or msg.get("published-print") or msg.get("published-online")
    if issued and isinstance(issued.get("date-parts"), list) and issued["date-parts"]:
        first = issued["date-parts"][0]
        if first and isinstance(first[0], int):
            year = first[0]
    return {
        "doi": (msg.get("DOI") or doi).lower(),
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "year": year,
        "type": msg.get("type", ""),
    }


def search_crossref_bibliographic(query: str, rows: int = 5) -> list[dict]:
    """Crossref REST: GET /works?query.bibliographic=<query>

    Feed the raw citation string AS-IS — e.g. "Landauer, R. (1961).
    Irreversibility and Heat Generation in the Computing Process. IBM J.
    Res. Dev. 5(3), 183-191." Crossref indexes the whole reference and
    returns ranked candidate works (DOI + title + authors + year).

    Returns up to ``rows`` candidate dicts in the same shape as
    fetch_crossref_metadata, with an extra ``score`` field (Crossref relevance
    score). Returns [] on miss / network error / very short query.
    """
    if not query or len(query.strip()) < 20:
        return []
    safe = urllib.parse.quote_plus(query.strip())
    url = f"https://api.crossref.org/works?query.bibliographic={safe}&rows={rows}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ICSAC-pipeline/1.0 (mailto:info@icsacinstitute.org)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CITATION_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    items = (data.get("message") or {}).get("items") or []
    out = []
    for msg in items:
        title_list = msg.get("title") or []
        title = title_list[0].strip() if title_list else ""
        if not title:
            continue
        abstract = msg.get("abstract") or ""
        if abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()
        authors = []
        for a in msg.get("author", []) or []:
            family = (a.get("family") or "").strip()
            given = (a.get("given") or "").strip()
            full = (f"{given} {family}").strip() or family or given
            if full:
                authors.append(full)
        year = None
        issued = msg.get("issued") or msg.get("published-print") or msg.get("published-online")
        if issued and isinstance(issued.get("date-parts"), list) and issued["date-parts"]:
            first = issued["date-parts"][0]
            if first and isinstance(first[0], int):
                year = first[0]
        out.append({
            "doi": (msg.get("DOI") or "").lower(),
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "year": year,
            "type": msg.get("type", ""),
            "score": msg.get("score", 0.0),
        })
    return out


def search_semanticscholar(query: str) -> list[dict]:
    """Semantic Scholar Graph API search.

    Up to 5 candidates, fields: paperId, title, authors, year, abstract,
    externalIds. Free + key-less; UA carries the institutional address
    so S2 routes us into their polite-pool quota.

    Returns a list of result dicts (possibly empty). Network or parse
    errors collapse to an empty list — caller treats as "no match".
    """
    if not query:
        return []
    params = urllib.parse.urlencode({
        "query": query[:500],
        "limit": "5",
        "fields": "title,authors,year,abstract,externalIds",
    })
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ICSAC-pipeline/1.0 (mailto:info@icsacinstitute.org)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CITATION_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    results = data.get("data") or []
    if not isinstance(results, list):
        return []
    return results
