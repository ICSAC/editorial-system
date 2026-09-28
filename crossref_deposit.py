"""Crossref content registration for the ICSAC submission pipeline.

ICSAC is a Crossref member (prefix 10.67697, since 2026-07-24). An accepted
paper published on icsacinstitute.org gets a DOI under that prefix. The Zenodo
path in repository_deposit.py stays for the pre-membership backfile and is no
longer the default registrar -- see config.DOI_REGISTRAR.

Crossref has no draft concept and a DOI is permanent the moment a deposit is
processed, so the draft lives HERE, in two steps:

  stage(sub_dir, submission)     assign the DOI string, build the deposit XML,
                                 validate it against the Crossref 5.4.0 schema,
                                 write <sub_dir>/crossref/{deposit.xml,doi.json}.
                                 Sends NOTHING. Idempotent on the DOI once assigned.
  register(sub_dir, live=False)  POST the staged XML. Default = the Crossref TEST
                                 system (test.crossref.org), which registers
                                 nothing real; live=True = doi.crossref.org.
                                 Live order: publications page push (the DOI must
                                 resolve somewhere) -> deposit -> poll the
                                 submission log until every record reads Success
                                 -> state.json -> author "published" email.

Content type is config.CROSSREF_CONTENT_TYPE: "journal-article" (default since
2026-09-28: an article of Persistence, the Institute's journal of record, in the
open volume from registration day; the print edition is added by redeposit),
"report-paper" (a paper the Institute publishes outside the journal; carries an
explicit <publisher>) or "posted_content" (Crossref's preprint/working-paper
class -- OpenAlex labels those "preprint").

urllib + xml.etree only, matching the rest of the pipeline; lxml is used for
schema validation at stage time (fail closed: an invalid deposit never lands
on disk as a staged draft).
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import config  # noqa: E402

CR_NS = "http://www.crossref.org/schema/5.4.0"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
JATS_NS = "http://www.ncbi.nlm.nih.gov/JATS1"
AI_NS = "http://www.crossref.org/AccessIndicators.xsd"
SCHEMA_LOCATION = f"{CR_NS} https://www.crossref.org/schemas/crossref5.4.0.xsd"
for _p, _u in (("", CR_NS), ("xsi", XSI_NS), ("jats", JATS_NS), ("ai", AI_NS)):
    ET.register_namespace(_p, _u)

DEPOSIT_URL_LIVE = "https://doi.crossref.org/servlet/deposit"
DEPOSIT_URL_TEST = "https://test.crossref.org/servlet/deposit"
RESULT_URL_LIVE = "https://doi.crossref.org/servlet/submissionDownload"
RESULT_URL_TEST = "https://test.crossref.org/servlet/submissionDownload"

LICENSE_URLS = {
    "cc-by-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "cc-by-sa-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "cc0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
}


class CrossrefError(RuntimeError):
    """Raised when staging or registration cannot complete. Nothing partial is
    left behind on stage(); register() leaves state.json untouched on failure."""


def _cfg(name: str, default=None):
    return getattr(config, name, default)


def _q(tag: str, ns: str = CR_NS) -> str:
    return f"{{{ns}}}{tag}"


def _sub(parent: ET.Element, tag: str, text: Optional[str] = None,
         attrib: Optional[dict] = None, ns: str = CR_NS) -> ET.Element:
    el = ET.SubElement(parent, _q(tag, ns), attrib or {})
    if text is not None:
        el.text = text
    return el


# ── DOI assignment ─────────────────────────────────────────────────────────────

def _next_seq(seq_file: Path) -> int:
    """Monotonic per-year sequence, fcntl-locked like the submission counter.
    Flushed + fsynced BEFORE the lock is released (audit 2026-09-27 item 6: the old order
    unlocked with the write still buffered). A corrupt counter is refused, not
    silently reset to zero -- a reset would re-issue suffixes."""
    seq_file.parent.mkdir(parents=True, exist_ok=True)
    year = _dt.date.today().year
    with open(seq_file, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read().strip()
            if raw:
                try:
                    state = json.loads(raw)
                except ValueError:
                    raise CrossrefError(f"DOI counter {seq_file} is corrupt ({raw[:40]!r}); "
                                        f"refusing to reset it -- repair by hand")
            else:
                state = {}
            seq = int(state.get(str(year), 0)) + 1
            state[str(year)] = seq
            fh.seek(0); fh.truncate()
            fh.write(json.dumps(state))
            fh.flush(); os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return seq


def assign_doi(sub_dir: Path, *, explicit: Optional[str] = None) -> str:
    """Return the DOI for this submission, assigning one on first call.
    Persisted in <sub_dir>/crossref/doi.json so re-staging never burns a
    second suffix. `explicit` pins a DOI (dry runs, or a curator override)."""
    cr_dir = sub_dir / "crossref"
    doi_json = cr_dir / "doi.json"
    if sub_dir.name.startswith("ICSAC-SUB-TEST-") and not explicit:
        # Test-tier submissions never touch the production counter (audit 2026-09-27 item 7).
        explicit = f"{_cfg('CROSSREF_PREFIX', '10.67697')}/TEST.{sub_dir.name}"
    if doi_json.exists():
        prior = json.loads(doi_json.read_text())
        if explicit and explicit != prior.get("doi"):
            raise CrossrefError(
                f"{sub_dir.name} already has DOI {prior['doi']}; refusing to reassign "
                f"to {explicit}. Delete crossref/doi.json first if that is really intended."
            )
        return prior["doi"]
    if explicit:
        doi = explicit
    else:
        prefix = _cfg("CROSSREF_PREFIX")
        if not prefix:
            raise CrossrefError("config.CROSSREF_PREFIX is not set")
        pattern = _cfg("CROSSREF_DOI_SUFFIX", "icsac.{year}.{seq:03d}")
        seq_file = Path(_cfg("CROSSREF_SEQ_FILE",
                             os.path.expanduser("~/icsac-submissions/.doi-seq")))
        seq = _next_seq(seq_file)
        doi = f"{prefix}/" + pattern.format(year=_dt.date.today().year, seq=seq,
                                             sub_id=sub_dir.name)
    if not re.fullmatch(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", doi):
        raise CrossrefError(f"DOI {doi!r} is not well-formed")
    cr_dir.mkdir(parents=True, exist_ok=True)
    doi_json.write_text(json.dumps({"doi": doi,
                                    "assigned_at": _now_iso(),
                                    "sub_id": sub_dir.name}, indent=2) + "\n")
    return doi


# ── Metadata → XML ─────────────────────────────────────────────────────────────

LICENSE_LABELS = {
    "cc-by-4.0": "CC BY 4.0",
    "cc-by-sa-4.0": "CC BY-SA 4.0",
    "cc0-1.0": "CC0 1.0",
}


def display_name(full: str) -> str:
    """'[author] [author]' / '[author], [author]' / '[author] [author]' -> '[author] [author]'."""
    if "," in (full or ""):
        last, _, first = full.partition(",")
        given, surname = first.strip(), last.strip()
        if surname.isupper() and len(surname) > 1:
            surname = surname.title()
    else:
        given, surname = split_name(full)
    return f"{given} {surname}".strip()


def split_name(full: str) -> tuple[str, str]:
    """(given, surname). An ALL-CAPS token marks the surname (French/EU form
    '[author] [author]'); otherwise the last token. Surname is title-cased
    when it arrived in caps so the record reads as prose, not as a form."""
    if "," in (full or ""):
        last, _, first = full.partition(",")
        surname, given = last.strip(), first.strip()
        return given, (surname.title() if surname.isupper() and len(surname) > 1 else surname)
    toks = [t for t in (full or "").split() if t]
    if not toks:
        return "", ""
    def _is_caps(t):  # a real ALL-CAPS word, not an initial like "M." or "J.R."
        core = t.replace(".", "").replace("-", "")
        return len(core) > 1 and core.isalpha() and core.isupper() and "." not in t
    caps = [t for t in toks if _is_caps(t)]
    if caps:
        # ALL-CAPS tokens are the surname wherever they sit ("[author] [author]",
        # "[author] [author]", "DE LA CRUZ Maria"); the rest is the given name.
        surname = " ".join(t.title() for t in caps)
        given = " ".join(t for t in toks if not _is_caps(t))
    else:
        surname, given = toks[-1], " ".join(toks[:-1])
    return given, surname


def _contributors(parent: ET.Element, submission: dict) -> None:
    form = submission.get("form", {})
    auth = submission.get("auth", {})
    # Intake writes the author list as `creators` (name, orcid, affiliation as the
    # author typed them). Fall back to the verified submitter identity.
    authors = submission.get("creators") or submission.get("authors") or [{
        "name": auth.get("name_on_record") or form.get("name", ""),
        "orcid": auth.get("orcid") or form.get("orcid"),
        "orcid_verified": bool(auth.get("verified")),
    }]
    contribs = _sub(parent, "contributors")
    for i, a in enumerate(authors):
        given, surname = split_name(a.get("name", "") if isinstance(a, dict) else str(a))
        if not surname:
            continue
        pn = _sub(contribs, "person_name", attrib={
            "sequence": "first" if i == 0 else "additional",
            "contributor_role": "author"})
        if given:
            _sub(pn, "given_name", given)
        _sub(pn, "surname", surname)
        aff = (a.get("affiliation") or "").strip() if isinstance(a, dict) else ""
        if aff:
            inst = _sub(_sub(pn, "affiliations"), "institution")
            _sub(inst, "institution_name", aff)
        orcid = (a.get("orcid") if isinstance(a, dict) else None) or ""
        orcid = orcid.strip().replace("https://orcid.org/", "")
        if re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
            # authenticated="true" is only honest when THIS iD came through
            # ORCID OAuth at intake: it must equal auth.orcid and auth.verified
            # must be set. Any other author's iD is asserted, not authenticated.
            verified_id = (auth.get("orcid") or "").strip().replace("https://orcid.org/", "")
            authed = bool(auth.get("verified")) and orcid == verified_id
            _sub(pn, "ORCID", f"https://orcid.org/{orcid}", attrib={
                "authenticated": "true" if authed else "false"})


def _abstract(parent: ET.Element, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    ab = ET.SubElement(parent, _q("abstract", JATS_NS),
                       {"{http://www.w3.org/XML/1998/namespace}lang": "en"})
    for para in [p for p in re.split(r"\n\s*\n", text) if p.strip()]:
        _sub(ab, "p", " ".join(para.split()), ns=JATS_NS)


def _date(parent: ET.Element, tag: str, d: _dt.date, media_type: Optional[str] = "online") -> None:
    attrib = {"media_type": media_type} if media_type else {}
    pd = _sub(parent, tag, attrib=attrib)
    _sub(pd, "month", f"{d.month:02d}")
    _sub(pd, "day", f"{d.day:02d}")
    _sub(pd, "year", str(d.year))


def _license(parent: ET.Element, license_id: str) -> None:
    url = LICENSE_URLS.get((license_id or "").lower())
    if not url:
        return
    prog = ET.SubElement(parent, _q("program", AI_NS), {"name": "AccessIndicators"})
    ET.SubElement(prog, _q("free_to_read", AI_NS))
    ref = ET.SubElement(prog, _q("license_ref", AI_NS), {"applies_to": "vor"})
    ref.text = url


def _doi_data(parent: ET.Element, doi: str, landing: str, pdf_url: Optional[str]) -> None:
    dd = _sub(parent, "doi_data")
    _sub(dd, "doi", doi)
    _sub(dd, "resource", landing)
    if pdf_url:
        # Similarity Check crawler + text-and-data-mining link, both to the PDF.
        col = _sub(dd, "collection", attrib={"property": "crawler-based"})
        item = _sub(col, "item", attrib={"crawler": "iParadigms"})
        _sub(item, "resource", pdf_url)
        col = _sub(dd, "collection", attrib={"property": "text-mining"})
        item = _sub(col, "item")
        _sub(item, "resource", pdf_url, attrib={"mime_type": "application/pdf"})


def _citations(parent: ET.Element, citations: list[dict]) -> None:
    """Verified DOIs go in as <doi>; the rest as unstructured text. Crossref
    matches unstructured citations itself, so nothing verifiable is lost."""
    if not citations:
        return
    cl = _sub(parent, "citation_list")
    for i, c in enumerate(citations, 1):
        cit = _sub(cl, "citation", attrib={"key": f"ref{i}"})
        doi = (c.get("doi") or "").strip()
        if doi and c.get("verified"):
            _sub(cit, "doi", doi)
        else:
            raw = " ".join((c.get("raw") or c.get("title") or "").split())
            if raw:
                _sub(cit, "unstructured_citation", raw[:2000])


def build_deposit_xml(submission: dict, *, doi: str, landing_url: str,
                      pdf_url: Optional[str], citations: Optional[list[dict]] = None,
                      content_type: Optional[str] = None,
                      publication_date: Optional[_dt.date] = None,
                      acceptance_date: Optional[_dt.date] = None,
                      batch_id: Optional[str] = None) -> bytes:
    """Serialise one Crossref deposit for one paper. Pure: no I/O."""
    content_type = content_type or _cfg("CROSSREF_CONTENT_TYPE", "journal-article")
    pub_date = publication_date or _dt.date.today()
    title = " ".join((submission.get("title") or "").split())
    if not title:
        raise CrossrefError("submission has no title")
    now = _dt.datetime.now(_dt.timezone.utc)
    batch_id = batch_id or f"{submission.get('sub_id', 'icsac')}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"

    root = ET.Element(_q("doi_batch"), {
        "version": "5.4.0",
        _q("schemaLocation", XSI_NS): SCHEMA_LOCATION,
    })
    head = _sub(root, "head")
    _sub(head, "doi_batch_id", batch_id)
    _sub(head, "timestamp", now.strftime("%Y%m%d%H%M%S"))
    dep = _sub(head, "depositor")
    _sub(dep, "depositor_name", _cfg("CROSSREF_DEPOSITOR_NAME", "ICSAC"))
    _sub(dep, "email_address", _cfg("CROSSREF_DEPOSITOR_EMAIL", "help@icsacinstitute.org"))
    _sub(head, "registrant", _cfg("CROSSREF_REGISTRANT",
                                  "Institute for Complexity Science and Advanced Computing"))
    body = _sub(root, "body")

    if content_type == "report-paper":
        rp = _sub(body, "report-paper")
        md = _sub(rp, "report-paper_metadata", attrib={"language": "en"})
        _contributors(md, submission)
        _sub(_sub(md, "titles"), "title", title)
        _abstract(md, submission.get("abstract", ""))
        _date(md, "publication_date", pub_date)
        pub = _sub(md, "publisher")
        _sub(pub, "publisher_name", _cfg("CROSSREF_PUBLISHER_NAME",
                                         "Institute for Complexity Science and Advanced Computing"))
        place = _cfg("CROSSREF_PUBLISHER_PLACE", "Fort Wayne, Indiana, USA")
        if place:
            _sub(pub, "publisher_place", place)
        _license(md, submission.get("license", ""))
        _doi_data(md, doi, landing_url, pdf_url)
        _citations(md, citations or [])
    elif content_type == "journal-article":
        # Persistence is the journal of record, published online at
        # icsacinstitute.org: every accepted paper is an article of the open
        # volume from registration day (publication_date media_type=online).
        # The paperback and ebook are the volume's annual edition; the print
        # date and pages are added later by redepositing the same DOI. The
        # eISSN is pending (LoC refile >= 2026-11-12; "ISSN Pending" explicitly
        # allowed) -- journal_metadata without an ISSN is schema-valid; add
        # CROSSREF_JOURNAL_ISSN when issued. Schema order is fixed:
        # journal_metadata, journal_issue (the volume), journal_article.
        jr = _sub(body, "journal")
        jm = _sub(jr, "journal_metadata", attrib={"language": "en"})
        _sub(jm, "full_title", _cfg("CROSSREF_JOURNAL_TITLE", "Persistence"))
        abbrev = _cfg("CROSSREF_JOURNAL_ABBREV", "")
        if abbrev:
            _sub(jm, "abbrev_title", abbrev)
        issn = _cfg("CROSSREF_JOURNAL_ISSN", "")
        if issn:
            _sub(jm, "issn", issn, attrib={"media_type": "electronic"})
        volume = str(_cfg("CROSSREF_JOURNAL_VOLUME", "") or "").strip()
        if volume:
            # journal_issue requires a publication_date: the volume's online
            # year from config, else the article's own year.
            vol_year = str(_cfg("CROSSREF_JOURNAL_VOLUME_YEAR", "") or "").strip()
            if not (len(vol_year) == 4 and vol_year.isdigit()):
                vol_year = str(pub_date.year)
            ji = _sub(jr, "journal_issue")
            _sub(_sub(ji, "publication_date", attrib={"media_type": "online"}), "year", vol_year)
            _sub(_sub(ji, "journal_volume"), "volume", volume)
        ja = _sub(jr, "journal_article", attrib={"publication_type": "full_text", "language": "en"})
        _sub(_sub(ja, "titles"), "title", title)
        _contributors(ja, submission)
        _abstract(ja, submission.get("abstract", ""))
        _date(ja, "publication_date", pub_date)
        acc = acceptance_date
        if acc:
            _date(ja, "acceptance_date", acc, media_type=None)
        seq_txt = doi.rsplit(".", 1)[-1]
        if seq_txt.isdigit():
            pi = _sub(ja, "publisher_item")
            _sub(pi, "item_number", seq_txt, attrib={"item_number_type": "article_number"})
        _license(ja, submission.get("license", ""))
        _doi_data(ja, doi, landing_url, pdf_url)
        _citations(ja, citations or [])
    elif content_type == "posted_content":
        pc = _sub(body, "posted_content", attrib={"type": _cfg("CROSSREF_POSTED_TYPE", "other"),
                                                   "language": "en"})
        _contributors(pc, submission)
        _sub(_sub(pc, "titles"), "title", title)
        _date(pc, "posted_date", pub_date, media_type=None)
        _abstract(pc, submission.get("abstract", ""))
        _license(pc, submission.get("license", ""))
        _doi_data(pc, doi, landing_url, pdf_url)
        _citations(pc, citations or [])
    else:
        raise CrossrefError(f"unsupported CROSSREF_CONTENT_TYPE {content_type!r}")

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ── Schema validation ─────────────────────────────────────────────────────────

def validate_xml(xml_bytes: bytes) -> None:
    """Validate against crossref5.4.0.xsd (config.CROSSREF_SCHEMA_DIR). Raises
    CrossrefError with every schema message on failure; ImportError if lxml is
    missing, so a missing validator can never pass as a valid deposit."""
    from lxml import etree  # in requirements.txt since 2026-09-27
    schema_dir = Path(_cfg("CROSSREF_SCHEMA_DIR",
                           os.path.expanduser("~/Desktop/icsac/crossref-schemas")))
    xsd = schema_dir / "crossref5.4.0.xsd"
    if not xsd.exists():
        raise CrossrefError(f"schema not found: {xsd}")
    try:
        schema = etree.XMLSchema(etree.parse(str(xsd)))
    except (etree.XMLSchemaParseError, etree.XMLSyntaxError, OSError) as e:
        raise CrossrefError(f"cannot load Crossref schema from {schema_dir}: {e}")
    try:
        doc = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        raise CrossrefError(f"deposit is not well-formed XML: {e}")
    if not schema.validate(doc):
        msgs = [f"line {e.line}: {e.message}" for e in schema.error_log]
        raise CrossrefError("deposit XML failed schema validation:\n  " + "\n  ".join(msgs))


# ── Stage (the draft) ─────────────────────────────────────────────────────────

def _load_citations(sub_id: str) -> list[dict]:
    p = Path(_REPO_ROOT) / "reviews" / f"{sub_id}_citations.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("citations", []) or []
    except (ValueError, OSError):
        return []


def landing_url_for(submission: dict, sub_dir: Path) -> str:
    """The URL the DOI will resolve to. A slug already registered in
    /publications wins; otherwise the slug publications.make_slug would mint,
    so the page and the deposit agree once register() pushes the entry."""
    import publications
    state_p = sub_dir / "state.json"
    if state_p.exists():
        slug = json.loads(state_p.read_text()).get("publications_slug")
        if slug:
            return publications.publications_url(slug)
    return publications.publications_url(publications.make_slug(submission.get("title", "")))


def stage(sub_dir: Path, submission: Optional[dict] = None, *,
          doi: Optional[str] = None, out_dir: Optional[Path] = None,
          content_type: Optional[str] = None,
          log: Callable[[str], None] = print) -> dict:
    """Build + validate + write the draft deposit. Returns {doi, xml_path,
    landing_url, pdf_url, content_type}. With out_dir set (dry run) the DOI
    counter is NOT advanced and nothing is written under sub_dir."""
    sub_dir = Path(sub_dir)
    submission = submission or json.loads((sub_dir / "submission.json").read_text())
    sub_id = submission.get("sub_id") or sub_dir.name
    if out_dir is not None:
        if not doi:
            doi = f"{_cfg('CROSSREF_PREFIX', '10.67697')}/DRYRUN.{sub_id}"
        target = Path(out_dir)
    else:
        doi = assign_doi(sub_dir, explicit=doi)
        target = sub_dir / "crossref"
    landing = landing_url_for(submission, sub_dir)
    pdf_url = _cfg("CROSSREF_PDF_URL", "https://icsacinstitute.org/papers/{sub_id}.pdf")
    pdf_url = pdf_url.format(sub_id=sub_id) if pdf_url else None
    acc = None
    try:
        st = json.loads((sub_dir / "state.json").read_text())
        if st.get("decision") == "accept" and st.get("completed_at"):
            acc = _dt.date.fromisoformat(st["completed_at"][:10])
    except Exception:
        acc = None
    xml_bytes = build_deposit_xml(submission, doi=doi, landing_url=landing, pdf_url=pdf_url,
                                  citations=_load_citations(sub_id), content_type=content_type,
                                  acceptance_date=acc)
    validate_xml(xml_bytes)  # raises -> nothing written
    target.mkdir(parents=True, exist_ok=True)
    xml_path = target / "deposit.xml"
    xml_path.write_bytes(xml_bytes)
    (target / "staged.json").write_text(json.dumps({
        "doi": doi, "landing_url": landing, "pdf_url": pdf_url,
        "content_type": content_type or _cfg("CROSSREF_CONTENT_TYPE", "journal-article"),
        "staged_at": _now_iso(), "registered": False,
    }, indent=2) + "\n")
    log(f"  crossref: staged draft for {sub_id} -> {xml_path} (doi {doi}, {len(xml_bytes)} bytes, schema OK)")
    return {"doi": doi, "xml_path": str(xml_path), "landing_url": landing,
            "pdf_url": pdf_url,
            "content_type": content_type or _cfg("CROSSREF_CONTENT_TYPE", "journal-article")}


# ── Register (the irreversible step) ──────────────────────────────────────────

def _creds() -> tuple[str, str]:
    email = _cfg("CROSSREF_LOGIN_EMAIL", "") or os.environ.get("CROSSREF_LOGIN_EMAIL", "")
    role = _cfg("CROSSREF_ROLE", "") or os.environ.get("CROSSREF_ROLE", "")
    pw = _cfg("CROSSREF_PASSWORD", "") or os.environ.get("CROSSREF_PASSWORD", "")
    if not (email and role and pw):
        raise CrossrefError("CROSSREF_LOGIN_EMAIL / CROSSREF_ROLE / CROSSREF_PASSWORD not set "
                            "(expected in ~/.config/crossref.env)")
    return f"{email}/{role}", pw


def _multipart(fields: dict, file_field: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----icsac-" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/xml\r\n\r\n").encode()
    out += file_bytes + b"\r\n" + f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def deposit(xml_bytes: bytes, *, live: bool, filename: str = "deposit.xml",
            log: Callable[[str], None] = print) -> str:
    """POST one deposit. Returns the doi_batch_id. Crossref accepts the file
    synchronously (HTTP 200 + an HTML receipt) and processes it asynchronously;
    poll_result() reads the outcome."""
    import urllib.request, urllib.error
    login, pw = _creds()
    url = DEPOSIT_URL_LIVE if live else DEPOSIT_URL_TEST
    body, ctype = _multipart({"operation": "doMDUpload", "login_id": login, "login_passwd": pw},
                             "fname", filename, xml_bytes)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("User-Agent", "icsac-editorial-system/1.0 (help@icsacinstitute.org)")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            page = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise CrossrefError(f"deposit HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")
    if "SUCCESS" not in page.upper() and "successfully" not in page.lower():
        raise CrossrefError(f"deposit not acknowledged: {page[:400]}")
    batch_id = ET.fromstring(xml_bytes).find(f".//{_q('doi_batch_id')}").text
    log(f"  crossref: {'LIVE' if live else 'TEST'} deposit accepted, batch {batch_id}")
    return batch_id


def poll_result(batch_id: str, *, live: bool, expect_doi: Optional[str] = None,
                timeout_s: int = 900,
                log: Callable[[str], None] = print) -> dict:
    """Read the submission log until the batch is processed. Returns
    {status, success, failure, warning, messages}. Raises on failure records."""
    import urllib.request, urllib.parse, urllib.error
    login, pw = _creds()
    url = RESULT_URL_LIVE if live else RESULT_URL_TEST
    params = urllib.parse.urlencode({"usr": login, "pwd": pw, "doi_batch_id": batch_id, "type": "result"})
    deadline = time.time() + timeout_s
    delay = 15
    while True:
        req = urllib.request.Request(f"{url}?{params}")
        req.add_header("User-Agent", "icsac-editorial-system/1.0 (help@icsacinstitute.org)")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise CrossrefError(f"result poll HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
        text = raw.decode(errors="replace")
        try:
            doc = ET.fromstring(raw)
        except ET.ParseError:
            doc = None
        status = doc.get("status") if doc is not None and doc.tag == "doi_batch_diagnostic" else None
        if status == "completed":
            recs = doc.findall("record_diagnostic")
            msgs = [(r.get("status"), (r.findtext("doi") or ""), (r.findtext("msg") or "").strip()) for r in recs]
            out = {"status": status,
                   "success": int(doc.findtext("batch_data/success_count") or 0),
                   "warning": int(doc.findtext("batch_data/warning_count") or 0),
                   "failure": int(doc.findtext("batch_data/failure_count") or 0),
                   "messages": msgs}
            for st, d, m in msgs:
                log(f"  crossref: record {d}: {st} {m}")
            if out["failure"] or any(st != "Success" for st, _, _ in msgs):
                raise CrossrefError(f"batch {batch_id} processed with failures: {msgs}")
            if not msgs or out["success"] < 1:
                raise CrossrefError(f"batch {batch_id} completed with no Success records: {text[:300]}")
            if expect_doi and expect_doi.lower() not in {d.lower() for st, d, _ in msgs if st == "Success"}:
                raise CrossrefError(f"batch {batch_id} completed but {expect_doi} is not among its "
                                    f"Success records: {msgs}")
            return out
        if time.time() > deadline:
            raise CrossrefError(f"batch {batch_id} not processed after {timeout_s}s; last: {text[:200]}")
        log(f"  crossref: batch {batch_id} {status or 'queued'}; retry in {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 120)


def register(sub_dir: Path, *, live: bool = False, override_window: bool = False,
             log: Callable[[str], None] = print) -> dict:
    """The irreversible step, made resumable (rewritten after the 2026-09-27 audit).

    TEST (default): deposit the staged XML to test.crossref.org and poll. No state
    written beyond a test note in staged.json; nothing public changes.

    LIVE, in this order, each step checkpointed in crossref/staged.json so a
    re-run RESUMES at the first incomplete step instead of refusing or repeating:
      0. a persisted curator ACCEPT is required (state.json decision == "accept")
      1. re-stage the XML under the reserved DOI: publication_date = today and the
         landing slug re-derived -- the deposit describes registration day
      2. Crossref deposit; poll until OUR DOI reads Success   -> crossref_registered_at
      3. /publications push (+ PDF, + public review record)    -> publications_pushed_at
      4. Zenodo archive publish (preflight: draft carries our DOI) -> zenodo_published_at
      5. author "published" notice DRAFTED to Gmail             -> author_drafted_at
      6. one curator ping that says what actually happened
    Crossref before the landing page: a page showing a DOI that never registered is
    worse than a DOI that resolves to a 404 for the few minutes the site builds.
    """
    sub_dir = Path(sub_dir)
    cr = sub_dir / "crossref"
    xml_path, staged_p = cr / "deposit.xml", cr / "staged.json"
    if not xml_path.exists() or not staged_p.exists():
        raise CrossrefError(f"nothing staged for {sub_dir.name}; run stage first")
    staged = json.loads(staged_p.read_text())
    submission = json.loads((sub_dir / "submission.json").read_text())
    state_p = sub_dir / "state.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    doi = staged["doi"]
    is_test_sub = bool(submission.get("test_mode")) or sub_dir.name.startswith("ICSAC-SUB-TEST-")

    def _save_staged():
        staged_p.write_text(json.dumps(staged, indent=2, default=str) + "\n")

    if not live:
        validate_xml(xml_path.read_bytes())
        batch_id = deposit(xml_path.read_bytes(), live=False, filename=f"{sub_dir.name}.xml", log=log)
        result = poll_result(batch_id, live=False, expect_doi=doi, log=log)
        staged["last_test"] = {"batch_id": batch_id, "at": _now_iso(), "result": result}
        _save_staged()
        return {"doi": doi, "batch_id": batch_id, "live": False, **result}

    # ── LIVE ──────────────────────────────────────────────────────────────
    if is_test_sub:
        raise CrossrefError("refusing a LIVE registration for a test-tier submission")
    if state.get("decision") != "accept":
        raise CrossrefError(f"{sub_dir.name} has no curator ACCEPT on record "
                            f"(state.decision={state.get('decision')!r}); run intake/decide.sh "
                            f"{sub_dir.name} accept first. The machine never decides.")
    # The author's word, or the window's close (2026-09-27). Withdraw/hold refuse.
    from intake import author_approval
    ok, why = author_approval.gate(sub_dir, override_window=override_window)
    if not ok:
        raise CrossrefError(f"{sub_dir.name}: {why}")
    log(f"  crossref: author gate: {why}")
    ck = staged.setdefault("checkpoints", {})
    outcome = {"doi": doi, "live": True, "resumed": bool(ck)}
    try:
        # 1. fresh XML for registration day (skip once Crossref has it)
        if not ck.get("crossref_registered_at"):
            stage(sub_dir, submission, doi=doi, log=log)
            staged = json.loads(staged_p.read_text()); ck = staged.setdefault("checkpoints", {})
        xml_bytes = xml_path.read_bytes()
        validate_xml(xml_bytes)

        # 2. Crossref
        if not ck.get("crossref_registered_at"):
            batch_id = deposit(xml_bytes, live=True, filename=f"{sub_dir.name}.xml", log=log)
            ck["crossref_batch_id"] = batch_id; ck["crossref_deposited_at"] = _now_iso(); _save_staged()
            result = poll_result(batch_id, live=True, expect_doi=doi, log=log)
            ck["crossref_registered_at"] = _now_iso(); ck["crossref_result"] = result
            staged["registered"] = True; staged["registered_at"] = ck["crossref_registered_at"]
            _save_staged()
            state.update({"deposit_doi": doi, "deposit_url": f"https://doi.org/{doi}",
                          "crossref_registered_at": ck["crossref_registered_at"],
                          "crossref_batch_id": batch_id, "registrar": "crossref"})
            state_p.write_text(json.dumps(state, indent=2))
        else:
            log(f"  crossref: {doi} already registered at {ck['crossref_registered_at']} "
                f"(batch {ck.get('crossref_batch_id')}); resuming")
        outcome["batch_id"] = ck.get("crossref_batch_id")

        # 3. landing page
        if not ck.get("publications_pushed_at"):
            entry = _push_publications(sub_dir, submission, doi, log=log)
            ck["publications_pushed_at"] = _now_iso()
            ck["publications_slug"] = entry.get("slug") if entry else None
            _save_staged()
            state = json.loads(state_p.read_text())
        outcome["landing"] = state.get("publications_url")

        # 4. Zenodo archive under the same DOI
        if not ck.get("zenodo_published_at"):
            archive_url = _publish_zenodo_archive(sub_dir, doi, log=log)
            ck["zenodo_published_at"] = _now_iso(); ck["archive_url"] = archive_url or "none"
            _save_staged()
            if archive_url:
                state["archive_url"] = archive_url
                state_p.write_text(json.dumps(state, indent=2))
        outcome["archive"] = ck.get("archive_url")

        # 5. author notice, DRAFTED
        if not ck.get("author_drafted_at"):
            ok = _notify_author(sub_dir, submission, doi, state.get("publications_slug"),
                                archive_url=None if ck.get("archive_url") in (None, "none") else ck.get("archive_url"),
                                log=log)
            if ok:
                ck["author_drafted_at"] = _now_iso()
            else:
                ck["author_draft_error"] = f"draft failed at {_now_iso()}"
            _save_staged()
        outcome["author_draft"] = "drafted" if ck.get("author_drafted_at") else "FAILED -- write it by hand"
    except Exception:
        _save_staged()
        raise

    # 6. the truthful ping
    _ping(f"DOI REGISTERED for {sub_dir.name}: https://doi.org/{doi}\n"
          f"Landing: {outcome.get('landing') or '(publications not configured)'}\n"
          f"Zenodo archive: {outcome.get('archive')}\n"
          f"Author 'published' email: {outcome['author_draft']}"
          + (" (Gmail Drafts -- review and send by hand)" if ck.get("author_drafted_at") else "")
          + ("\n(resumed from an earlier partial run)" if outcome["resumed"] else ""))
    return {**outcome, **(ck.get("crossref_result") or {})}


def _publish_zenodo_archive(sub_dir: Path, doi: str, *, log) -> Optional[str]:
    """Publish the Zenodo draft staged at accept. PREFLIGHT first (audit 2026-09-27 item 3): the
    remote draft must carry OUR DOI as its external DOI, or we refuse -- publishing
    a draft without it would make Zenodo mint 10.5281/… and put two DOIs on one
    paper. Idempotent: an already-published record is returned, not re-published.
    Returns the record URL, or None when no draft was staged."""
    state_p = sub_dir / "state.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    record_id = state.get("deposit_record_id")
    if not record_id:
        log("  crossref: no Zenodo draft staged for this submission; skipping archive publish")
        return None
    import repository_deposit
    dep = repository_deposit._request_json(
        "GET", f"{config.ZENODO_API}/deposit/depositions/{record_id}", token=config.ZENODO_TOKEN)
    remote_doi = ((dep.get("metadata") or {}).get("doi") or dep.get("doi") or "").lower()
    if remote_doi != doi.lower():
        _ping(f"HELD {sub_dir.name}: Zenodo draft {record_id} carries DOI {remote_doi!r}, not {doi}. "
              f"Not publishing it -- fix the draft's DOI (or re-stage) and re-run register-doi.sh --live.")
        raise CrossrefError(f"Zenodo draft {record_id} DOI is {remote_doi!r}, expected {doi}; refusing to publish")
    if dep.get("submitted") or dep.get("state") == "done":
        url = (dep.get("links") or {}).get("record_html") or (dep.get("links") or {}).get("html") \
              or f"https://zenodo.org/records/{record_id}"
        log(f"  crossref: Zenodo record {record_id} is already published ({url}); nothing to do")
        return url
    res = repository_deposit.publish_draft(str(record_id), log=log)
    got = (res.get("doi") or "").lower()
    if got != doi.lower():
        _ping(f"ALARM {sub_dir.name}: Zenodo published record {record_id} under DOI {got!r}, "
              f"expected {doi}. Two DOIs now point at one paper -- needs your hands.")
        raise CrossrefError(f"Zenodo published under {got!r}, expected {doi}")
    log(f"  crossref: Zenodo archive live at {res.get('record_url')} under {doi}")
    return res.get("record_url")


def _ping(msg: str) -> None:
    """Curator Telegram; never raises."""
    try:
        import notify
        notify.send_to_curator(msg, parse_mode=None)
    except Exception as exc:
        print(f"  crossref: curator ping failed: {exc}", file=sys.stderr)


def _landing(slug: str) -> str:
    import publications
    return publications.publications_url(slug)


def _push_publications(sub_dir: Path, submission: dict, doi: str, *, log) -> dict:
    """Upsert /publications, copy the PDF to the website tree, stage the public
    review, push. Mirrors publish_watcher._register_published for Zenodo."""
    import shutil
    import publications
    from publish_watcher import _proto_authors_from_submission
    sub_id = sub_dir.name
    proto = {
        "title": submission.get("title") or "Untitled",
        "authors": _proto_authors_from_submission(submission),
        "doi": doi,
        "abstract": (submission.get("abstract") or "")[:2000],
        "source": "submission-pdf",
        "source_ref": f"https://doi.org/{doi}",
        "sub_id": sub_id,
    }
    lic = LICENSE_URLS.get((submission.get("license") or "").lower())
    if lic:
        proto["license_url"] = lic   # the landing page shows the licence + copyright line
    st_now = json.loads((sub_dir / "state.json").read_text()) if (sub_dir / "state.json").exists() else {}
    if st_now.get("promotion_opt_out"):
        proto["promotion_opt_out"] = True   # honoured by any promotion tooling reading the registry
    if st_now.get("author_exclusions"):
        proto["promotion_exclusions"] = [x for x in st_now["author_exclusions"] if x != "persistence"]
    if st_now.get("persistence_opt_out"):
        proto["persistence_opt_out"] = True
    entry = publications.upsert_entry(proto)
    if not entry:
        log("  crossref: publications registry not configured; landing page NOT pushed")
        return {}
    extra = []
    if publications.WEBSITE_REPO:
        pdf_src = sub_dir / "paper.pdf"
        if pdf_src.exists():
            dest = Path(publications.WEBSITE_REPO) / "public" / "papers" / f"{sub_id}.pdf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pdf_src, dest)
            extra.append(str(dest))
    review_md, rqc_md = publications.stage_public_review_for_slug(
        sub_id, entry["slug"], config.REVIEWS_DIR)   # (review_key, slug, dir) -- same call as publish_watcher
    publications.commit_and_push(message=f"publications: {entry['title']} ({entry['slug']}) — {doi}",
                                 extra_paths=extra or None)
    state_p = sub_dir / "state.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    state["publications_slug"] = entry["slug"]
    state["publications_url"] = publications.publications_url(entry["slug"])
    state_p.write_text(json.dumps(state, indent=2))
    log(f"  crossref: publications entry pushed -> {state['publications_url']}")
    return entry


def _notify_author(sub_dir: Path, submission: dict, doi: str, slug: Optional[str], *,
                   archive_url: Optional[str] = None, log) -> bool:
    """DRAFTS the author's published notice (Gmail Drafts) and says whether it did.
    Nothing author-facing is sent automatically except the intake receipt."""
    try:
        from intake import notify_author
        import publications
        form = submission.get("form", {})
        landing = publications.publications_url(slug) if slug else f"https://doi.org/{doi}"
        test_mode = bool(submission.get("test_mode")) or sub_dir.name.startswith("ICSAC-SUB-TEST-")
        tier = 1
        if test_mode:
            try:
                tier = 2 if int(submission.get("tier") or 0) == 2 else 3
            except (TypeError, ValueError):
                tier = 3
        try:  # ask once, and never an author who excluded newsletters or already belongs;
              # unknown consent (unreadable record, registry down) = no optional ask
            from intake import post_publication
            stay = post_publication.wants_stay_involved(sub_dir, submission)
        except Exception:
            stay = False
        ok, info = notify_author.send_published(
            to=form["email"], sub_id=sub_dir.name, title=submission.get("title") or "",
            author_name=form.get("name", ""), deposit_doi=doi,
            deposit_url=archive_url or landing,
            publications_url=landing,
            license_name=LICENSE_LABELS.get((submission.get("license") or "").lower(), ""),
            stay_involved=stay, tier=tier)
        log(f"  crossref: author published-notice {'DRAFTED' if ok else 'FAILED: ' + str(info)}")
        return bool(ok)
    except Exception as exc:  # the DOI is registered either way; never mask that
        log(f"  crossref: author notice crashed (non-fatal): {exc}")
        return False


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _resolve(sub_id: str) -> Path:
    from intake.submission_worker import _resolve_sub_dir
    sub_dir, _tier = _resolve_sub_dir(sub_id)
    if not sub_dir.is_dir():
        raise CrossrefError(f"no such submission: {sub_dir}")
    return sub_dir


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="crossref_deposit.py", description=__doc__.split("\n\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("stage", help="build + validate the draft deposit (sends nothing)")
    s.add_argument("sub_id"); s.add_argument("--doi"); s.add_argument("--out", help="dry run: write here, don't touch the counter")
    s.add_argument("--content-type", choices=["journal-article", "report-paper", "posted_content"])
    r = sp.add_parser("register", help="deposit the staged XML (TEST system unless --live)")
    r.add_argument("sub_id"); r.add_argument("--live", action="store_true")
    r.add_argument("--override-window", action="store_true",
                   help="register before the author's objection window closes (never past a hold or withdrawal)")
    v = sp.add_parser("validate", help="schema-check an XML file"); v.add_argument("path")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "stage":
            res = stage(_resolve(a.sub_id), doi=a.doi, out_dir=Path(a.out) if a.out else None,
                        content_type=a.content_type)
            print(json.dumps(res, indent=2))
        elif a.cmd == "register":
            if a.live:
                print(f"LIVE registration of {a.sub_id}: registers a permanent DOI at Crossref, "
                      f"publishes the Zenodo archive draft under it, pushes /publications, and "
                      f"drafts the author's notice. Type the sub id to confirm: ", end="", flush=True)
                if input().strip() != a.sub_id:
                    print("aborted"); return 2
            try:
                res = register(_resolve(a.sub_id), live=a.live, override_window=a.override_window)
            except Exception as e:  # ANY live failure reaches the curator, not only ours (audit 2026-09-27 item 4)
                if a.live:
                    _ping(f"DOI registration FAILED for {a.sub_id}: {type(e).__name__}: {str(e)[:300]}\n"
                          f"Re-run intake/register-doi.sh {a.sub_id} --live to resume from the last checkpoint.")
                if isinstance(e, CrossrefError):
                    raise
                print(f"crossref: {type(e).__name__}: {e}", file=sys.stderr); return 1
            print(json.dumps(res, indent=2, default=str))
        elif a.cmd == "validate":
            validate_xml(Path(a.path).read_bytes()); print("schema OK")
    except CrossrefError as e:
        print(f"crossref: {e}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
