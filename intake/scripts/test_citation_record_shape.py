#!/usr/bin/env python3
"""A text search must not report a neighbour of the cited work as the work.

Offline: every registry call is stubbed, and any call that reaches an
unstubbed lookup is a failure. Nothing is written. Run from the repo root:
    .venv/bin/python intake/scripts/test_citation_record_shape.py

A registry search for a book finds its reviews first. A review's registered
title is the book's title, often followed by the author, publisher, page count
and price; it shares the year; and a review record may credit the book's
author, so title, author and year can all agree. The same searches propose one
chapter of a book, a later re-posting of a paper, supplementary material, or a
Choice Reviews entry. Each case below was reported as found by an earlier
verifier. All fixtures are synthetic.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


import citation_verify as cv  # noqa: E402
import submission_intake as si  # noqa: E402

leaks: list[str] = []


def _leak(name):
    def f(*a, **k):
        leaks.append(name)
        return [] if name.startswith("search") else None
    return f


si.fetch_crossref_metadata = _leak("fetch_crossref_metadata")
si.search_semanticscholar = _leak("search_semanticscholar")
si.fetch_arxiv_metadata = _leak("fetch_arxiv_metadata")
cv._fetch_datacite = lambda doi: None
cv._doi_handle_status = lambda doi: "registered"

BOOK_RAW = "Penhallow, A. (1971). A Field Guide to Quiet Machines. Example University Press."
BOOK = {"raw": BOOK_RAW, "authors": ["Penhallow"], "title": "A Field Guide to Quiet Machines",
        "year": 1971, "doi": None, "arxiv_id": None, "type": "title-only", "claim_context": ""}
BOOK_REC = {"doi": "10.0000/book.1971.1", "title": "A Field Guide to Quiet Machines",
            "authors": ["Ada Penhallow"], "abstract": "", "year": 1971, "type": "monograph",
            "container": "", "score": 50.0}
ART_RAW = "Penhallow, A. (2015). Quiet gears and loud clocks. Journal of Examples, 12(3), 45-67."
ART = {"raw": ART_RAW, "authors": ["Penhallow"], "title": "Quiet gears and loud clocks",
       "year": 2015, "doi": None, "arxiv_id": None, "type": "title-only", "claim_context": ""}
ART_CR = {"resolver": "crossref", "resolved_id": "10.0000/joe.2015.12", "title": "Quiet gears and loud clocks",
          "abstract": "", "year": 2015, "authors": ["Ada Penhallow"], "type": "journal-article",
          "container": "Journal of Examples"}

# What each stub returns for the case being run
S = {"arxiv": None, "s2": None, "biblio": None, "crossref": {}, "books": []}
calls = {"books": 0}


def _books(query, rows=5, type_filter=None, raise_errors=False):
    if not type_filter:
        leaks.append("search_crossref_bibliographic (unfiltered)")
        return []
    calls["books"] += 1
    return [dict(b) for b in S["books"]]


cv._search_arxiv = lambda *a, **k: (dict(S["arxiv"]) if S["arxiv"] else None)
cv._search_semanticscholar = lambda *a, **k: (dict(S["s2"]) if S["s2"] else None)
cv._search_crossref_bibliographic = lambda *a, **k: (dict(S["biblio"]) if S["biblio"] else None)
cv._fetch_crossref = lambda doi: (dict(S["crossref"][doi]) if doi in S["crossref"] else None)
si.search_crossref_bibliographic = _books


def run(c, arxiv=None, s2=None, biblio=None, crossref=None, books=None):
    S.update(arxiv=arxiv, s2=s2, biblio=biblio, crossref=crossref or {}, books=books or [])
    return cv.verify_citation(dict(c))


def found(out, rid):
    return bool(out.get("verified")) and out.get("resolved_id") == rid


print("1. a review whose title adds author, publisher, page count and price")
review_priced = {"resolver": "crossref", "resolved_id": "10.0000/qex.1971.88",
                 "title": "A Field Guide to Quiet Machines, by Ada Penhallow, Example University Press, "
                          "Exampleton, 1971, 240 pp., $7.50",
                 "abstract": "", "year": 1971, "authors": ["R. Critic"], "type": "journal-article",
                 "container": "Quarterly of Examples"}
out = run(BOOK, biblio=review_priced, books=[BOOK_REC])
check(not found(out, "10.0000/qex.1971.88"), "the priced review is not reported as the book")
check(found(out, "10.0000/book.1971.1"), "the book record is found instead")
check(any(x.get("resolved_id") == "10.0000/qex.1971.88" for x in out.get("rejected_candidates") or []),
      "the refused review is kept on the record with its reason")

print("2. a review with the book's exact title, crediting the book's author")
calls["books"] = 0
review_plain = {"resolver": "semanticscholar", "resolved_id": "10.0000/gaz.1972.7",
                "title": "A Field Guide to Quiet Machines", "abstract": "", "year": 1972,
                "authors": ["Ada Penhallow"]}
review_plain_cr = {"resolver": "crossref", "resolved_id": "10.0000/gaz.1972.7",
                   "title": "A Field Guide to Quiet Machines", "abstract": "", "year": 1972,
                   "authors": ["Ada Penhallow"], "type": "journal-article", "container": "Gazette of Examples"}
out = run(BOOK, s2=review_plain, biblio=dict(review_plain_cr), crossref={"10.0000/gaz.1972.7": review_plain_cr})
check(not out.get("verified"), "a journal article in a journal the reference never names is not the book")
check(str(out.get("reason", "")).startswith("Not matched"), "the reference stays unmatched and says why")
check("journal article" in str(out.get("reason", "")), "the reason names what the record is")
check(calls["books"] >= 1, "a book-record search was tried")

print("3. a Choice Reviews entry")
choice = {"resolver": "semanticscholar", "resolved_id": "10.5860/choice.00-0001",
          "title": "A Field Guide to Quiet Machines", "abstract": "", "year": 1971, "authors": ["Ada Penhallow"]}
out = run(BOOK, s2=choice, books=[BOOK_REC])
check(not found(out, "10.5860/choice.00-0001"), "a Choice Reviews entry is not the book")
check(found(out, "10.0000/book.1971.1"), "the book record is found instead")

print("4. one chapter of the cited book")
chapter = {"resolver": "crossref", "resolved_id": "10.0000/book.1971.1_5",
           "title": "A Field Guide to Quiet Machines: Counting Gears", "abstract": "", "year": 1971,
           "authors": ["Ada Penhallow"], "type": "book-chapter", "container": "A Field Guide to Quiet Machines"}
out = run(BOOK, biblio=chapter, books=[BOOK_REC])
check(not found(out, "10.0000/book.1971.1_5"), "a chapter is not the whole book, even when its host book carries the cited title")
check(found(out, "10.0000/book.1971.1"), "the book record is found instead")

no_title = {"raw": "Lind, T., & Quarrie, I. (1980). Field notes on gears (3rd ed.). Example Press.",
            "authors": ["Lind", "Quarrie"], "title": None, "year": 1980, "doi": None, "arxiv_id": None,
            "type": "unstructured", "claim_context": ""}
part = {"resolver": "crossref", "resolved_id": "10.0000/fng.1980.4", "title": "COUNTING GEARS",
        "abstract": "", "year": 1980, "authors": ["Tomas Lind", "Iryna Quarrie"], "type": "book-chapter",
        "container": "Field Notes on Gears"}
out = run(no_title, biblio=part)
check(not found(out, "10.0000/fng.1980.4"),
      "with no cited title, naming the host book does not make one of its chapters the cited work")

print("5. a later re-posting of a journal article")
repost = {"resolver": "semanticscholar", "resolved_id": "10.0000/repost.2024.9",
          "title": "Quiet Gears and Loud Clocks", "abstract": "", "year": 2024, "authors": ["Ada Penhallow"]}
repost_cr = dict(repost, resolver="crossref", type="posted-content", container="")
out = run(ART, s2=repost, biblio=ART_CR, crossref={"10.0000/repost.2024.9": repost_cr})
check(not found(out, "10.0000/repost.2024.9"), "a posting nine years after the cited year is not the cited article")
check(found(out, "10.0000/joe.2015.12"), "the next resolver's journal record is found instead")

print("6. supplementary material")
supp = {"resolver": "semanticscholar", "resolved_id": "f00dfeed0000000000000000000000000000beef",
        "title": "Supporting Online Material for Quiet Gears and Loud Clocks", "abstract": "", "year": 2015,
        "authors": ["Ada Penhallow"]}
out = run(ART, s2=supp, biblio=ART_CR)
check(not found(out, "f00dfeed0000000000000000000000000000beef"), "supplementary material is not the article")
check(found(out, "10.0000/joe.2015.12"), "the article is found instead")

print("7. right matches stay right")
mono = {"resolver": "crossref", "resolved_id": "10.0000/book.1971.1", "title": "A Field Guide to Quiet Machines",
        "abstract": "", "year": 1971, "authors": ["Ada Penhallow"], "type": "monograph", "container": ""}
out = run(BOOK, biblio=mono)
check(found(out, "10.0000/book.1971.1"), "a book cited as a book still matches its book record")
long_sub = {"resolver": "crossref", "resolved_id": "10.0000/jtf.1995.155",
            "title": "Wire lattices in thin films. A second ordering mechanism appears in every sample we grew",
            "abstract": "", "year": 1995, "authors": ["Tomas Lind"], "type": "journal-article",
            "container": "Journal of Thin Films"}
cit = {"raw": "Lind, T. (1995). Wire lattices in thin films. Journal of Thin Films, 155(3), 1151-1164.",
       "authors": ["Lind"], "title": "Wire lattices in thin films", "year": 1995, "doi": None,
       "arxiv_id": None, "type": "title-only", "claim_context": ""}
out = run(cit, biblio=long_sub)
check(found(out, "10.0000/jtf.1995.155"), "a journal article with a long real subtitle still matches")
named = {"resolver": "crossref", "resolved_id": "10.0000/exl.2019.3", "title": "Small gears", "abstract": "",
         "year": 2019, "authors": ["Ada Penhallow"], "type": "journal-article", "container": "Example Gazette"}
cit = {"raw": "Penhallow, A. (2019). Small gears. Example Gazette.", "authors": ["Penhallow"],
       "title": "Small gears", "year": 2019, "doi": None, "arxiv_id": None, "type": "title-only",
       "claim_context": ""}
out = run(cit, biblio=named)
check(found(out, "10.0000/exl.2019.3"), "a journal article still matches when the reference names its journal")
preprint = {"resolver": "arxiv", "resolved_id": "arXiv:1401.00001", "title": "Quiet gears and loud clocks",
            "abstract": "", "year": 2014, "authors": ["Ada Penhallow"], "type": "posted-content", "container": "arXiv"}
out = run(ART, arxiv=preprint)
check(found(out, "arXiv:1401.00001"), "the preprint of a cited article (a year earlier) still matches")

serial = {"resolver": "crossref", "resolved_id": "10.0000/pex.2013.4", "title": "Gears in Winter", "abstract": "",
          "year": 2013, "authors": ["Tomas Lind"], "type": "book-chapter", "container": "Progress in Examples"}
cit = {"raw": 'Lind, T. "Gears in winter." Seminar Notes XII, 101-130 (2013).', "authors": ["Lind"],
       "title": "Gears in winter", "year": 2013, "doi": None, "arxiv_id": None, "type": "title-only",
       "claim_context": ""}
out = run(cit, biblio=serial)
check(found(out, "10.0000/pex.2013.4"),
      "a contribution cited with volume and pages still matches the chapter a serial is registered as")

print("7b. other reference styles keep their matches")
def art(rid, title, cont, typ="journal-article", year=2021):
    return {"resolver": "crossref", "resolved_id": rid, "title": title, "abstract": "", "year": year,
            "authors": ["Tomas Lind"], "type": typ, "container": cont}
def ref(raw, title, year=2021):
    return {"raw": raw, "authors": ["Lind"], "title": title, "year": year, "doi": None, "arxiv_id": None,
            "type": "title-only", "claim_context": ""}
out = run(ref("Lind T. Gear trains in cold rooms. Clin Eng Rev. 2021.", "Gear trains in cold rooms"),
          biblio=art("10.0000/cer.2021.1", "Gear trains in cold rooms", "Clinical Engineering Review"))
check(found(out, "10.0000/cer.2021.1"), "a journal abbreviated without periods (Vancouver style) reads as a journal")
out = run(ref("[1] T. Lind, Gear trains on streams, ICML, 2021.", "Gear trains on streams"),
          biblio=art("10.0000/pmlr.2021.2", "Gear trains on streams", "Proceedings of Machine Learning Research",
                     typ="proceedings-article"))
check(found(out, "10.0000/pmlr.2021.2"), "a conference named by its acronym reads as a conference paper")
out = run(ref("Lind, T. (2020). Trends in gear wear. Technical Report, Example Institute.", "Trends in gear wear", 2020),
          biblio=art("10.0000/rep.2020.3", "Trends in gear wear", "", typ="posted-content", year=2020))
check(found(out, "10.0000/rep.2020.3"), "a technical report still matches its posted record")
out = run(ref("Lind, T. (2019). The road. Journal of Examples, 3(1), 1-9.", "The road", 2019),
          biblio=art("10.0000/joe.2019.4", "The Road: By Night", "Journal of Examples", year=2019))
check(found(out, "10.0000/joe.2019.4"), "a subtitle that begins with \"By\" is not a review byline")
out = run(ref("Lind, T. (2001). Gears. Choice Reviews Online, 39(2), 1055.", "Gears", 2001),
          biblio=art("10.5860/choice.39-1055x", "Gears", "Choice Reviews Online", year=2001))
check(found(out, "10.5860/choice.39-1055x"), "a reference that cites a Choice entry still matches it")

print("8a. the book search wants the same book, not the same author's other book")
other_book = dict(BOOK_REC, doi="10.0000/book.1988.2", title="The Complexity of Quiet Machines: Models of Gears",
                  year=1988)
cit = dict(BOOK, raw="Penhallow, A. (1971). The Evolution of Quiet Machines. Example University Press.",
           title="The Evolution of Quiet Machines")
rev = dict(review_priced, title="The Evolution of Quiet Machines, by Ada Penhallow, Example University Press, "
                                "1971, 240 pp., $7.50")
out = run(cit, biblio=rev, books=[other_book])
check(not found(out, "10.0000/book.1988.2"), "a book sharing most title words with the cited one is not it")
check(not out.get("verified"), "with no book record of that title, the reference stays unmatched")

print("8b. an edited book is found by its editor")
edited = {"doi": "10.0000/book.1998.3", "title": "The Economics of Gears", "authors": [], "editors": ["Tomas Lind"],
          "abstract": "", "year": 1998, "type": "edited-book", "container": "", "score": 40.0}
cit = {"raw": "Lind, T. (Ed.) (1998). The Economics of Gears. Example University Press.", "authors": ["Lind"],
       "title": "The Economics of Gears", "year": 1998, "doi": None, "arxiv_id": None, "type": "title-only",
       "claim_context": ""}
rev = dict(review_priced, resolved_id="10.0000/qex.1999.5", year=1999,
           title="The Economics of Gears. Edited by Tomas Lind. Example University Press, 1998. Pp. 307. $49.95")
out = run(cit, biblio=rev, books=[edited])
check(found(out, "10.0000/book.1998.3"), "the edited book is found through its editor")

print("8c. a book search that could not run is not \"no book record\"")
def _books_down(query, rows=5, type_filter=None, raise_errors=False):
    if raise_errors:
        raise TimeoutError("registry did not answer")
    return []
cv.BOOK_SEARCH_RETRY_WAIT = 0
si.search_crossref_bibliographic = _books_down
out = run(BOOK, biblio=review_priced)
flaky = {"n": 0}
def _books_flaky(query, rows=5, type_filter=None, raise_errors=False):
    flaky["n"] += 1
    if flaky["n"] == 1:
        raise TimeoutError("registry did not answer")
    return [dict(BOOK_REC)]
si.search_crossref_bibliographic = _books_flaky
out2 = run(BOOK, biblio=review_priced)
si.search_crossref_bibliographic = _books
check(found(out2, "10.0000/book.1971.1") and flaky["n"] == 2, "a refused book-record search is retried once")
check(not out.get("verified") and "not checked" in str(out.get("reason", "")),
      "the reason says the book-record search could not be run")
check("found none" not in str(out.get("reason", "")), "it does not claim that no book record exists")

print("8. the suggestion after a dead DOI is never a review")
dead = dict(BOOK, doi="10.0000/nowhere.1", type="doi")
cv._doi_handle_status = lambda doi: "unregistered"
out = run(dead, biblio=review_priced)
check(out.get("doi_identity") == "dead", "the dead DOI is still a finding")
check(out.get("suggested_id") != "10.0000/qex.1971.88", "a review is not offered as the intended record")
cv._doi_handle_status = lambda doi: "registered"

print("9. a re-run that finds the earlier record is a review withdraws it")
earlier = dict(BOOK, verified=True, confidence="title-year-match", resolver="crossref",
               resolved_id="10.0000/qex.1971.88")          # the paper's cited title, as a re-run sends it
S.update(arxiv=None, s2=None, biblio=dict(review_priced), crossref={}, books=[])
merged = cv.verify_all([earlier], max_concurrent=1)[0]
check(not merged.get("verified"), "the earlier match to a priced review is withdrawn, not kept as verified")
earlier2 = dict(BOOK, verified=True, confidence="title-author-match", resolver="semanticscholar",
                resolved_id="10.0000/gaz.1972.7", title="A Field Guide to Quiet Machines")
S.update(arxiv=None, s2=review_plain, biblio=dict(review_plain_cr), crossref={"10.0000/gaz.1972.7": review_plain_cr},
         books=[])
merged = cv.verify_all([earlier2], max_concurrent=1)[0]
check(bool(merged.get("verified")) and merged.get("recheck_reason"),
      "a refusal on record type alone does not withdraw an earlier match (it is noted as a recheck)")

print("10. what a reference reads as")
shape = getattr(cv, "_cited_shape", None)
check(shape is not None and shape(BOOK)["book"], "a title and a publisher read as a book")
check(shape is not None and not shape(ART)["book"] and shape(ART)["journal"], "volume, issue and pages read as a journal article")
check(shape is not None and shape({"raw": "Lind, T. (1990). Gear trains. In B. Editor (Ed.), Clocks (pp. 1-20). "
                                          "Example Press.", "title": "Gear trains"})["chapter"],
      "In ... (Ed.) ... pp. reads as a chapter")
check(shape is not None and not shape({"raw": "Lind, T. (1990). The years 1815-1914: A survey. Example Press.",
                                       "title": None})["journal"],
      "a year range in a book title is not a page range")

check(not leaks, f"no unstubbed registry call ({sorted(set(leaks))})")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("ALL PASS")
