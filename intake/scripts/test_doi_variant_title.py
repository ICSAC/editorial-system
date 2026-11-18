#!/usr/bin/env python3
"""A DOI whose record has a different title is the same work only with evidence.

The verifier used to confirm any DOI whose record shared an author and a year
(within one) with the citation, calling the different title "a translated or
variant title". The same author's paper of the year before shares both, so a
wrong DOI was shown to the panel as CONFIRMED. Now the different title needs
evidence: shared title words, a record title in another language or script, or
the record's volume and pages in the citation. Otherwise it is a DOI MISMATCH
that names the record. Confirmed lines print the record's year. Offline: every
registry call is stubbed with the records' real metadata.

    .venv/bin/python intake/scripts/test_doi_variant_title.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import citation_verify as cv  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


# Crossref metadata as the registry returns it (container, volume, page).
REC = {
    "10.2307/1969529": {"resolver": "crossref", "resolved_id": "10.2307/1969529", "title": "Non-Cooperative Games",
                        "abstract": "", "year": 1951, "authors": ["John Nash"],
                        "container": "The Annals of Mathematics", "volume": "54", "page": "286"},
    "10.1007/bf01397280": {"resolver": "crossref", "resolved_id": "10.1007/bf01397280",
                           "title": "�ber den anschaulichen Inhalt der quantentheoretischen Kinematik und Mechanik",
                           "abstract": "", "year": 1927, "authors": ["W. Heisenberg"],
                           "container": "Zeitschrift f�r Physik", "volume": "43", "page": "172-198"},
    "10.1002/andp.19053221004": {"resolver": "crossref", "resolved_id": "10.1002/andp.19053221004",
                                 "title": "Zur Elektrodynamik bewegter Körper", "abstract": "", "year": 1905,
                                 "authors": ["A. Einstein"], "container": "Annalen der Physik",
                                 "volume": "322", "page": "891-921"},
}
cv._fetch_crossref = lambda doi: dict(REC[doi.lower()]) if doi.lower() in REC else None
cv._fetch_datacite = lambda doi: None
cv._doi_handle_status = lambda doi: "registered"
cv._search_arxiv = lambda *a, **k: None
cv._search_semanticscholar = lambda *a, **k: None
cv._search_crossref_bibliographic = lambda *a, **k: None


def cite(raw, authors, year, title, doi):
    return {"raw": raw, "authors": authors, "year": year, "title": title, "doi": doi,
            "arxiv_id": None, "type": "doi", "claim_context": ""}


print("[1] the same author's other paper, a year apart: DOI MISMATCH naming the record")
other = cite("Nash, J. F. (1950). The bargaining problem. Econometrica, 18(2), 155–162. "
             "https://doi.org/10.2307/1969529", ["J. F. Nash"], 1950, "The bargaining problem", "10.2307/1969529")
v = cv.verify_citation(dict(other))
check(v["verified"] is False, "not verified")
check(v.get("doi_identity") == "mismatch", f"doi_identity == mismatch ({v.get('doi_identity')})")
check("Non-Cooperative Games" in v.get("reason", "") and "(1951)" in v.get("reason", ""),
      "the reason names the record and its year")
check(not v.get("title_variant"), "not called a variant title")

print("[2] a translated title: still confirmed, with the evidence named")
heis = cite("Heisenberg, W. (1927). On the perceptual content of quantum theoretical kinematics and mechanics. "
            "Zeitschrift für Physik, 43(3–4), 172–198. https://doi.org/10.1007/BF01397280",
            ["W. Heisenberg"], 1927, "On the perceptual content of quantum theoretical kinematics and mechanics",
            "10.1007/BF01397280")
v = cv.verify_citation(dict(heis))
check(v["verified"] and v.get("doi_identity") == "confirmed" and v.get("title_variant"),
      "a German record cited by its English title is confirmed")
check(bool(v.get("title_variant_evidence")), f"evidence named: {v.get('title_variant_evidence')}")
heis_bare = dict(heis, raw="Heisenberg, W. (1927). On the perceptual content of quantum theoretical kinematics "
                           "and mechanics. Zeitschrift für Physik.")
v = cv.verify_citation(heis_bare)
check(v["verified"] and "another language" in (v.get("title_variant_evidence") or ""),
      f"with no pages cited, the language is the evidence ({v.get('title_variant_evidence')})")
eins = cite("Einstein, A. (1905). On the electrodynamics of moving bodies. Annalen der Physik.",
            ["A. Einstein"], 1905, "On the electrodynamics of moving bodies", "10.1002/andp.19053221004")
v = cv.verify_citation(dict(eins))
check(v["verified"] and "another language" in (v.get("title_variant_evidence") or ""),
      "one function word beside an accented letter is enough")
eins_pages = dict(eins, raw="Einstein, A. (1905). On the electrodynamics of moving bodies. Annalen der Physik, "
                            "17(10), 891–921.")
v = cv.verify_citation(eins_pages)
check(v["verified"] and "page range" in (v.get("title_variant_evidence") or ""),
      f"a renumbered volume (17 cited, 322 on record) still agrees on the page range ({v.get('title_variant_evidence')})")

print("[3] the record's volume and pages in the citation: confirmed despite the wrong title")
pages = cite("Nash, J. (1951). The bargaining problem. Annals of Mathematics, 54(2), 286–295.",
             ["J. Nash"], 1951, "The bargaining problem", "10.2307/1969529")
v = cv.verify_citation(dict(pages))
check(v["verified"] and v.get("title_variant")
      and "volume and first page" in (v.get("title_variant_evidence") or ""),
      f"volume + first page agree ({v.get('title_variant_evidence')})")

print("[3b] volume, issue and pp. forms")
pp = cite("Nash, J. (1951). The bargaining problem. Annals of Mathematics, Vol. 54, No. 2, pp. 286-295.",
          ["J. Nash"], 1951, "The bargaining problem", "10.2307/1969529")
v = cv.verify_citation(dict(pp))
check(v["verified"] and "volume and first page" in (v.get("title_variant_evidence") or ""),
      f"'Vol. 54, No. 2, pp. 286' agrees ({v.get('title_variant_evidence')})")

print("[3c] a translated title whose cited pages are another paper's: DOI MISMATCH")
other_de = cite("Heisenberg, W. (1926). Many-body problem and resonance in quantum mechanics. Zeitschrift für "
                "Physik, 38(6–7), 411–426. https://doi.org/10.1007/BF01397280", ["W. Heisenberg"], 1926,
                "Many-body problem and resonance in quantum mechanics", "10.1007/BF01397280")
v = cv.verify_citation(dict(other_de))
check(v["verified"] is False and v.get("doi_identity") == "mismatch",
      f"the 1927 record's pages are not the cited ones ({v.get('doi_identity')})")
check("not the record's" in v.get("reason", ""), "the reason says the pages disagree")

print("[3d] the other direction: an English record cited by a translated title")
pt = cite("Nash, J. (1951). Jogos não cooperativos. Annals of Mathematics.", ["J. Nash"], 1951,
          "Jogos não cooperativos", "10.2307/1969529")
v = cv.verify_citation(dict(pt))
check(v["verified"] and v.get("title_variant"), f"confirmed as a translated title ({v.get('title_variant_evidence')})")

print("[4] language and script reading")
tl = getattr(cv, "_title_language", None)
check(tl is not None and tl("The Bargaining Problem") == "en", "an English title reads as English")
check(tl is not None and tl("Zur Elektrodynamik bewegter Körper") == "other", "a German title reads as another language")
check(tl is not None and tl("Теория вероятностей") == "nonlatin", "a Cyrillic title reads as another script")
check(tl is not None and tl("Schrödinger cat states") == "", "an accented name alone is not a language")

print("[5] the report prints the record's year beside the record's title")
same = cite("Nash, J. (1950). Non-cooperative games. Annals of Mathematics, 54(2), 286–295.",
            ["J. Nash"], 1950, "Non-cooperative games", "10.2307/1969529")
res = cv.verify_all([dict(same), dict(other)])
rep = cv.build_verification_report(res)
line = next((l for l in rep.splitlines() if "CONFIRMED" in l and "10.2307/1969529" in l), "")
check("*Non-Cooperative Games* (1951)" in line, f"confirmed line: {line[:120]}")
check(re.search(r"Nash 1950\*\* — DOI MISMATCH", rep) is not None, "the other paper is listed as DOI MISMATCH")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
