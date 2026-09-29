"""Preprint DOI check (policy 2026-09-28).

A paper already posted as a preprint may be submitted: the preprint keeps its
DOI and the Persistence article gets its own under the Institute prefix, linked
to it (Crossref hasPreprint). Any preprint server counts. The check asks doi.org
which registration agency holds the DOI, then reads that agency's record:

  Crossref   preprints are type "posted-content", subtype "preprint", on every
             server checked on 2026-09-28 (Preprints.org, bioRxiv/medRxiv, SSRN,
             Research Square, ChemRxiv, TechRxiv, Authorea, the OSF family,
             EarthArXiv, engrXiv, Cambridge Open Engage, ESSOAr, JMIR Preprints,
             Beilstein Archives). A journal article, proceedings paper or book
             record is REFUSED: that work is published. So is a preprint whose
             record says it "is-preprint-of" a published version. Some prefixes
             (SSRN, JMIR, SciELO, Wiley) hold both, so the record decides, not
             the prefix.
  DataCite   arXiv and Zenodo preprints are resourceTypeGeneral "Preprint";
             other types are accepted and flagged for the curation team
             (Zenodo authors often file a preprint as "Journal article").
  other RAs  accepted and flagged (the record was not read).

A DOI that does not exist is refused. A registry that cannot be reached fails
OPEN: accepted, marked unverified and flagged; the curation team checks by hand.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "ICSAC-intake/1.0 (mailto:help@icsacinstitute.org)"
_ARXIV_ID = re.compile(r"^(?:arxiv:)?(?:10\.48550/arxiv\.)?(\d{4}\.\d{4,5})(?:v\d+)?$", re.I)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
PUBLISHED_TYPES = {
    "journal-article", "proceedings-article", "book-chapter", "book", "monograph",
    "edited-book", "reference-book", "book-part", "book-section", "book-track",
    "reference-entry",
}


class Refused(ValueError):
    """The DOI cannot be accepted as a preprint. The message is author-facing."""


def normalize(raw) -> str | None:
    """A bare DOI (arXiv ids become arXiv's 10.48550 DOI, version dropped), or None."""
    s = (raw or "").strip() if isinstance(raw, str) else ""
    s = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", s, flags=re.I).strip()
    m = _ARXIV_ID.match(s)
    if m:
        return f"10.48550/arXiv.{m.group(1)}"
    return s if _DOI.match(s) and len(s) <= 300 else None


def _get(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _words(text) -> set[str]:
    return set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))


def title_differs(a, b) -> bool:
    """True when two titles share under 40 % of their longer words."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) < 0.4


def check(doi: str, *, title: str = "", timeout: float = 10.0) -> dict:
    """Read the preprint's record. Returns {doi, ra, type, server, title,
    verified, flags}; raises Refused when the DOI cannot be a preprint."""
    out: dict = {"doi": doi, "ra": None, "type": None, "server": None, "title": None,
                 "verified": False, "flags": []}
    q = urllib.parse.quote(doi, safe="/")
    try:
        rows = _get(f"https://doi.org/ra/{q}", timeout)
    except Exception as exc:
        out["flags"].append(f"the DOI registries could not be reached ({type(exc).__name__}); "
                            f"check the preprint by hand")
        return out
    row = rows[0] if isinstance(rows, list) and rows else {}
    ra = row.get("RA") or ""
    if not ra:
        raise Refused(f"The preprint DOI {doi} does not exist. Check it, or leave the field empty.")
    out["ra"] = ra
    try:
        if ra == "Crossref":
            m = _get(f"https://api.crossref.org/works/{q}", timeout)["message"]
            typ = m.get("type") or ""
            sub = m.get("subtype")
            out["type"] = f"{typ}/{sub}" if sub else typ
            out["title"] = (m.get("title") or [None])[0]
            inst = m.get("institution") or []
            out["server"] = (inst[0].get("name") if inst else None) or m.get("publisher")
            if typ in PUBLISHED_TYPES:
                where = (m.get("container-title") or [None])[0] or m.get("publisher") or "a journal"
                raise Refused(
                    f"The DOI {doi} is a published {typ.replace('-', ' ')} ({where}), not a preprint. "
                    f"ICSAC publishes work that has not been published elsewhere; a preprint of it is fine.")
            published = (m.get("relation") or {}).get("is-preprint-of") or []
            if published:
                raise Refused(
                    f"The preprint {doi} is already linked to a published version "
                    f"({published[0].get('id')}). ICSAC publishes work that has not been published elsewhere.")
            if typ != "posted-content":
                out["flags"].append(f"Crossref type {typ}, not a preprint record")
            elif sub and sub != "preprint":
                out["flags"].append(f"posted content of subtype {sub}, not a preprint")
        elif ra == "DataCite":
            a = _get(f"https://api.datacite.org/dois/{q}", timeout)["data"]["attributes"]
            rtg = (a.get("types") or {}).get("resourceTypeGeneral")
            pub = a.get("publisher")
            out["type"] = rtg
            out["server"] = pub.get("name") if isinstance(pub, dict) else pub
            out["title"] = ((a.get("titles") or [{}])[0]).get("title")
            if rtg != "Preprint":
                out["flags"].append(f"DataCite type {rtg}, not marked as a preprint")
            # A published version, as arXiv records it (IsVersionOf the journal DOI).
            # The record's own prefix is skipped: Zenodo versions point at their
            # concept DOI that way.
            own = doi.split("/", 1)[0].lower()
            for rel in a.get("relatedIdentifiers") or []:
                if rel.get("relationType") not in ("IsVersionOf", "IsPreprintOf", "IsPublishedIn"):
                    continue
                if (rel.get("relatedIdentifierType") or "").upper() != "DOI":
                    continue
                target = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "",
                                (rel.get("relatedIdentifier") or "").strip(), flags=re.I)
                if not target or target.split("/", 1)[0].lower() == own:
                    continue
                try:
                    tm = _get(f"https://api.crossref.org/works/{urllib.parse.quote(target, safe='/')}",
                              timeout)["message"]
                except Exception:
                    out["flags"].append(f"linked to {target}; that record could not be read")
                    continue
                if (tm.get("type") or "") in PUBLISHED_TYPES:
                    where = (tm.get("container-title") or [None])[0] or tm.get("publisher") or "a journal"
                    raise Refused(
                        f"The preprint {doi} is already published ({where}, {target}). "
                        f"ICSAC publishes work that has not been published elsewhere.")
        else:
            out["flags"].append(f"registered with {ra}; the record was not read")
        out["verified"] = ra in ("Crossref", "DataCite")
    except Refused:
        raise
    except Exception as exc:
        out["flags"].append(f"the {ra} record could not be read ({type(exc).__name__}); check it by hand")
    if title and out["title"] and title_differs(title, out["title"]):
        out["flags"].append(f"the preprint's title differs: {out['title'][:150]!r}")
    return out
