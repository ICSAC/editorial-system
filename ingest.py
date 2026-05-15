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

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(dest_path, "wb") as out:
                while chunk := resp.read(8192):
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
# Kept here in ingest.py so DOI-source resolution stays centralized — both
# the watcher path (Zenodo only) and the intake path (Zenodo OR arXiv) use
# this module as the single ingestion surface. Pipeline's other modules
# (review, scrubber, etc.) accept any review_data dict matching the shape
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


def fetch_arxiv_metadata(arxiv_id: str) -> dict:
    """Fetch arXiv metadata via the Atom API. Returns a dict shaped like
    extract_review_data() output so review.review_paper can use it without
    branching on source.

    arXiv exposes no machine-readable license metadata (the per-deposit
    license is on the abstract page but not in the API). We leave the
    license slot empty; intake_server records the form-supplied license
    if any, otherwise the panel sees an empty license id.
    """
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "ICSAC-pipeline/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        atom = resp.read().decode("utf-8", errors="replace")

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
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(dest_path, "wb") as out:
                while chunk := resp.read(8192):
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
