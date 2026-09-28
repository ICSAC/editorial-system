"""Code and data availability check (policy of 2026-09-28).

ICSAC does not host authors' code or data. A paper that says its code or data
is available must say where: the submission form's code and data link, a
supplement related identifier, or a repository link in the paper's body. A paper that makes the claim with
no link anywhere is held for a revise-and-resubmit before the panel runs; the
curator signs that decision like every other one.

The check reads claims, not words. "Data" appears in nearly every paper, so
only an availability statement counts, and a statement that there is nothing
to share ("not applicable", "no new data were generated") does not. The
reference list is cut off first: links and phrases there belong to cited work.
"""
from __future__ import annotations

import re

CHECK_VERSION = 1

# A line that is only a references heading starts the reference list.
_REFS_HEADING = re.compile(
    r"^[ \t]*(?:\d+\.?[ \t]*)?(?:references|bibliography|works cited|literature cited)[ \t]*:?[ \t]*$",
    re.I | re.M,
)

_CLAIM_PATTERNS = [
    # "Data availability", "Code availability", "Data and code availability"
    r"\b(?:data|code|software)(?:\s*(?:,|and|&)\s*(?:data|code|software|materials?))*\s+availability\b",
    # "The code is available", "data will be deposited", "source code and
    # validated data files are available" (up to four words in between)
    r"\b(?:data|code|software|scripts?|notebooks?|datasets?)\b(?:\s+[\w-]+){0,4}?\s+(?:are|is|will\s+be)\s+"
    r"(?:publicly\s+|freely\s+|openly\s+|also\s+)?(?:available|provided|released|deposited|included|shared)\b",
    r"\bavailable\s+(?:up)?on\s+(?:reasonable\s+)?request\b",
    r"\b(?:accompanying|supplementary|supplemental)\s+(?:archive|code|data|datasets?|materials?|files?|"
    r"repository|notebooks?|scripts?|software)\b",
    r"\b(?:in|see)\s+the\s+supplement\b",
    r"\bprovided\s+(?:code|scripts?|notebooks?)\b",
    r"\b(?:archive|repository)\s+(?:contains|includes|provides)\b",
]
_CLAIM_RE = re.compile("|".join(f"(?:{p})" for p in _CLAIM_PATTERNS), re.I)
# The first pattern is a heading ("Data availability"); its answer follows it.
_HEADING_RE = re.compile(_CLAIM_PATTERNS[0], re.I)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")

# Said right after an availability heading, these mean there is nothing to link.
_NOTHING_TO_SHARE = re.compile(
    r"not\s+applicable|\bn/?a\b|no\s+(?:new\s+)?(?:data|code|datasets?)\b|"
    r"(?:were|was)\s+not\s+(?:generated|used|created|collected)|"
    r"does\s+not\s+(?:use|involve|generate|include)\s+(?:any\s+)?(?:data|code)",
    re.I,
)

# Public places code and data live. The link only has to be public; this list
# is how the check recognises one in the paper's own text. A link anywhere in
# the body counts (front matter often carries the repository line).
_LINK_RE = re.compile(
    r"(?:https?://|www\.)[^\s)\]>\"',;]*"
    r"(?:github\.com|gitlab\.com|bitbucket\.org|codeberg\.org|zenodo\.org|osf\.io|figshare\.com|"
    r"dataverse|kaggle\.com|colab\.research\.google\.com|huggingface\.co|softwareheritage\.org|"
    r"datadryad\.org|codeocean\.com|sourceforge\.net)[^\s)\]>\"',;]*"
    r"|\b10\.(?:5281/zenodo\.\d+|7910/DVN/[\w]+|6084/m9\.figshare\.[\d.]+|17605/OSF\.IO/\w+|5061/dryad\.\w+)",
    re.I,
)


def body_text(full_text: str) -> str:
    """The paper without its reference list (the last references heading on)."""
    matches = list(_REFS_HEADING.finditer(full_text or ""))
    if not matches:
        return full_text or ""
    return full_text[: matches[-1].start()]


def _sentence(text: str, start: int, end: int, cap: int = 240) -> str:
    left = max(text.rfind(". ", 0, start), text.rfind("\n\n", 0, start))
    left = 0 if left < 0 else left + 1
    right_candidates = [i for i in (text.find(". ", end), text.find("\n\n", end)) if i >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    s = re.sub(r"\s+", " ", text[left:right]).strip()
    return s if len(s) <= cap else s[: cap - 1].rstrip() + "…"


def find_claims(text: str) -> list[str]:
    """Availability statements in `text`, one quoted sentence each."""
    out: list[str] = []
    for m in _CLAIM_RE.finditer(text):
        after = text[m.end(): m.end() + 120]
        if not _HEADING_RE.fullmatch(m.group(0)):
            # "Code is available. No new data were generated." The second
            # sentence is about data; it does not take back the code claim.
            end = _SENTENCE_END.search(after)
            if end:
                after = after[: end.start()]
        if _NOTHING_TO_SHARE.search(after):
            continue
        quote = _sentence(text, m.start(), m.end())
        if quote not in out:
            out.append(quote)
    return out


def find_links(text: str) -> list[str]:
    return sorted({m.group(0).rstrip(".") for m in _LINK_RE.finditer(text)})


def assess(submission: dict, full_text: str) -> dict:
    """The record the worker saves as code_availability.json.

    missing_link is True only when the paper claims code or data and no link
    exists anywhere: not on the form, not as a supplement related identifier,
    and not in the paper's body.
    """
    body = body_text(full_text)
    claims = find_claims(body)
    code_data = submission.get("code_data") or {}
    form_url = (code_data.get("url") or "").strip() or None
    # Either direction counts: forms before 2026-09-28 only offered "is supplement to".
    related = [r.get("identifier", "") for r in (submission.get("related_identifiers") or [])
               if isinstance(r, dict) and r.get("relation") in ("isSupplementedBy", "isSupplementTo")
               and r.get("identifier")]
    in_paper = find_links(body) if claims else []
    missing = bool(claims) and not form_url and not related and not in_paper
    return {
        "check_version": CHECK_VERSION,
        "declared_available": code_data.get("available"),
        "claims": claims[:5],
        "links": {"form": form_url, "related": related, "in_paper": in_paper},
        "missing_link": missing,
    }
