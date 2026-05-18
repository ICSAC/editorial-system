"""Blind-review preprocessing: strip identifying metadata before panel review.

Removes author names, affiliations, contact info, ORCID iDs, acknowledgments,
funding statements, and the references list from a manuscript before the AI
panel sees it. Inline citation markers ([Smith 2024], [1], etc.) are
preserved verbatim — the panel still sees that a claim is cited, even if
the bibliography is not visible. Citation verification is conducted
upstream against the original (un-stripped) text.

Two intentions, one mechanism:
  1. Bias reduction. Standard double-blind review practice — the panel
     should not be influenced by author identity, institutional prestige,
     or funder branding.
  2. Token reduction. Acknowledgments, full reference lists, and author
     metadata commonly account for 20-40% of a paper's token count.
     Trimming them lets larger manuscripts fit the panel's per-route
     context budgets.

Implementation strategy (extract-not-echo, 2026-05-18 refactor):

  Gemini is asked to IDENTIFY spans to remove (short snippets, section
  start/end anchors) — never to echo the redacted manuscript back. Python
  performs the actual removal via string operations against the original
  text. This keeps gemini's output small (a few KB regardless of paper
  size) and avoids the output-truncation / content-filter trips that hit
  large-paper echo-style runs (see the DET-paper failure mode at 28K
  tokens: gemini's invalid-content retry exhaustion mid-echo).

  Tradeoff: if gemini returns a snippet that doesn't string-match in the
  original (whitespace drift, hyphenation, OCR artifacts), that category
  silently does not redact. Logged in the manifest as a `match_failures`
  list so curators can spot egregious cases.

Returns (redacted_text, manifest_dict). On gemini failure or empty extract
output, returns (original_text, {"_failure": "<reason>"}) — the worker
treats that as a non-fatal warning and proceeds with the un-stripped
paper. A failed compaction never blocks a real submission.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import config


EXTRACT_PROMPT = """You are preparing an academic manuscript for double-blind peer review.

Your job: IDENTIFY (do NOT echo) the spans that must be removed before the
panel reads the paper. Return a JSON object with the identified spans only.
Python code will perform the actual removal from the original text using
string matching against the snippets you return.

CRITICAL: Do NOT echo the manuscript. Do NOT summarize it. Return ONLY the
JSON object described below. Output must be small — a few KB at most.

REMOVE these categories:
  1. author_names: full names appearing in the title block, headers, footers
     (e.g. "Jane M. Doe"). Do NOT include surname-only inline citations.
  2. affiliations: institutional strings (e.g. "Institute of Example
     Studies, Example University").
  3. emails: any email addresses in the author block.
  4. orcids: ORCID iDs (16-digit, hyphenated; final char may be 'X').
  5. funding_statements: short statements naming grants / funders (e.g.
     "Funded by NSF grant 12345").
  6. acknowledgments_section: the entire acknowledgments section. Return
     a start_marker (the first 30-80 characters that begin the section,
     starting at the section heading like "Acknowledgments") and an
     end_marker (the last 20-50 characters of the section's final
     sentence, before the next section heading or end of document).
  7. references_section: the bibliography list at the end of the paper.
     Same start_marker / end_marker pattern. start_marker should begin
     with the heading ("References", "Bibliography", "Works Cited") and
     extend through the first reference's opening words. end_marker should
     be the last 20-50 characters of the final reference entry.

PRESERVE (do not list these — anything not enumerated above must stay):
  - Title (unless it literally contains an author's full name)
  - Abstract, introduction, methods, results, discussion, conclusion
  - Equations, figures, figure captions, tables
  - Inline citation markers in the body: [Smith 2024], [1], (Doe et al.,
    2023). Those are NOT in the references_section — they are in the
    flowing prose.
  - Section and subsection headings

Be CONSERVATIVE. When uncertain whether a span is author-identifying or
substantive, omit it from the lists below — better to leave an identifier
in than to risk a python-side string match that accidentally removes
methodology prose.

Snippets MUST be verbatim substrings of the input manuscript. Python will
do `text.replace(snippet, "")` for the short-snippet categories and
`text[start:end]` removal for the section_marker pairs. If your snippet is
not a verbatim match, the redaction silently no-ops for that span (logged
in the manifest's match_failures list).

Output schema (JSON only, no markdown fencing, no prose around it):

{
  "author_names": ["Jane M. Doe", "John Smith"],
  "affiliations": ["Institute of Example Studies, Example University, Anytown USA"],
  "emails": ["jdoe@example.edu"],
  "orcids": ["0009-0000-0000-0001"],
  "funding_statements": ["Funded by NSF grant 12345-67"],
  "acknowledgments_section": {
    "start_marker": "Acknowledgments\\n\\nWe thank Sarah Friend for helpful comments",
    "end_marker": "for hosting this work."
  },
  "references_section": {
    "start_marker": "References\\n\\n[1] Adams, A. (2020). The thing is important.",
    "end_marker": "Things Quarterly, 5(2), 99-110."
  }
}

If a category has nothing to remove, return an empty list or null for that
field. Do not omit fields.
"""


_EMPTY_MANIFEST = {
    "author_names": [],
    "affiliations": [],
    "emails": [],
    "orcids": [],
    "funding_statements": [],
    "acknowledgments_text": "",
    "references_count": 0,
    "references_section_chars": 0,
}


def _gemini_call(prompt_input: str, *, timeout_sec: int = 600) -> tuple[str, str, int]:
    """Invoke gemini-cli, returning (stdout, stderr, returncode).

    Sets GEMINI_CLI_TRUST_WORKSPACE so headless invocations don't trip the
    trusted-folders gate. Uses EXTRACT_PROMPT as -p; passes the manuscript
    on stdin.
    """
    env = {**os.environ, "GEMINI_CLI_TRUST_WORKSPACE": "true"}
    proc = subprocess.run(
        [config.GEMINI_CMD, "-p", EXTRACT_PROMPT],
        input=prompt_input,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=env,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _extract_json(raw: str) -> dict | None:
    """Pull a single JSON object out of gemini's stdout.

    gemini-cli prepends warning lines on first use (256-color, ripgrep
    missing, etc.). We tolerate those — find the first '{' and parse from
    there. If the output is wrapped in ```json ... ``` despite the prompt
    asking otherwise, strip those fences first.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


def _flexible_find(haystack: str, needle: str, start_at: int = 0) -> tuple[int, int]:
    """Find needle in haystack, tolerant of whitespace differences.

    Treats any run of whitespace in the needle as matching any run of
    whitespace in the haystack. Necessary because PDF text extraction
    introduces line breaks and indentation that don't match what gemini
    saw (gemini reads paragraphs as flowing text; pdftotext emits with
    PDF's actual line wrapping).

    Returns (start_idx, end_idx) in haystack, or (-1, -1) if no match.
    """
    if not needle:
        return -1, -1
    # Tokenize needle into non-whitespace word atoms. Build a regex that
    # matches those words separated by any whitespace.
    tokens = re.findall(r"\S+", needle)
    if not tokens:
        return -1, -1
    pattern = r"\s+".join(re.escape(tok) for tok in tokens)
    m = re.search(pattern, haystack[start_at:])
    if not m:
        return -1, -1
    return start_at + m.start(), start_at + m.end()


def _apply_removals(text: str, spans: dict) -> tuple[str, dict, list]:
    """Apply gemini-identified spans to the manuscript via string operations.

    Returns (redacted_text, manifest, match_failures).
    match_failures is a list of {"category": ..., "snippet": ...} dicts
    for spans that didn't match the input verbatim — surfaced in the
    manifest for curator visibility.
    """
    manifest = dict(_EMPTY_MANIFEST)
    match_failures: list[dict] = []

    # Short-snippet categories: exact string replace, all occurrences.
    # Risk: if a snippet is a substring of body prose, all occurrences get
    # zapped. Mitigation: prompt asks gemini for FULL names/affiliations
    # which are unlikely to recur in body text. Single-pass replace, not
    # iterative — gemini is the source of truth on what's identifying.
    for category in ("author_names", "affiliations", "emails", "orcids",
                     "funding_statements"):
        snippets = spans.get(category) or []
        if not isinstance(snippets, list):
            snippets = []
        removed_actual = []
        for s in snippets:
            if not isinstance(s, str) or not s.strip():
                continue
            if s in text:
                text = text.replace(s, "")
                removed_actual.append(s)
            else:
                match_failures.append({"category": category, "snippet": s[:120]})
        manifest[category] = removed_actual

    # Section categories: start/end marker pair, removed verbatim.
    for section_key, manifest_text_field, count_field, chars_field in (
        ("acknowledgments_section", "acknowledgments_text", None, None),
        ("references_section", None, "references_count", "references_section_chars"),
    ):
        section = spans.get(section_key) or {}
        if not isinstance(section, dict):
            continue
        start_marker = section.get("start_marker") or ""
        end_marker = section.get("end_marker") or ""
        if not (start_marker and end_marker):
            continue

        # Whitespace-tolerant search. Exact-match fast path first.
        s_idx = text.find(start_marker)
        if s_idx < 0:
            s_idx, _ = _flexible_find(text, start_marker)
        if s_idx < 0:
            match_failures.append({"category": section_key,
                                   "snippet": f"start_marker: {start_marker[:80]}"})
            continue
        # End marker is searched AFTER the start position.
        search_from = s_idx + 1
        e_start = text.find(end_marker, search_from)
        if e_start < 0:
            e_start, e_end = _flexible_find(text, end_marker, start_at=search_from)
        else:
            e_end = e_start + len(end_marker)
        if e_start < 0:
            match_failures.append({"category": section_key,
                                   "snippet": f"end_marker: {end_marker[:80]}"})
            continue
        chunk = text[s_idx:e_end]
        text = text[:s_idx] + text[e_end:]

        if manifest_text_field:
            manifest[manifest_text_field] = chunk
        if chars_field:
            manifest[chars_field] = len(chunk)
        if count_field:
            # Heuristic: count reference entries. Numbered styles ([1] or
            # "1. ") matched first; author-year styles fall back to
            # counting blank-line-separated paragraph blocks (skipping the
            # heading itself).
            n = len(re.findall(r"\n\s*\[\d+\]", chunk))
            if n == 0:
                n = len(re.findall(r"\n\s*\d+\.\s", chunk))
            if n == 0:
                paragraphs = [
                    p.strip()
                    for p in re.split(r"\n\s*\n", chunk)
                    if p.strip()
                    and not p.strip().lower().startswith(
                        ("references", "bibliography", "works cited"))
                ]
                n = len(paragraphs)
            manifest[count_field] = n

    return text, manifest, match_failures


def compact_paper(paper_text: str, *, log=None) -> tuple[str, dict]:
    """Strip author/identifier metadata from a manuscript for blind review.

    Returns (redacted_text, manifest). On any failure path the original
    text is returned with a manifest carrying a "_failure" reason — the
    caller treats that as a non-fatal warning and proceeds with the
    un-stripped paper. Compaction MUST NEVER block a submission.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg, file=sys.stderr)

    if not paper_text or not paper_text.strip():
        manifest = dict(_EMPTY_MANIFEST)
        manifest["_failure"] = "empty input"
        return paper_text, manifest

    try:
        stdout, stderr, rc = _gemini_call(paper_text)
    except subprocess.TimeoutExpired:
        _log("  compaction: gemini timed out; proceeding with un-stripped paper")
        manifest = dict(_EMPTY_MANIFEST)
        manifest["_failure"] = "gemini timeout"
        return paper_text, manifest
    except Exception as exc:
        _log(f"  compaction: gemini call raised {type(exc).__name__}: {exc}")
        manifest = dict(_EMPTY_MANIFEST)
        manifest["_failure"] = f"gemini exception: {type(exc).__name__}"
        return paper_text, manifest

    if rc != 0:
        # Gemini-cli failed but we may still have partial output. Surface
        # the rc so curators can investigate; treat as a failure.
        snippet = (stderr or "")[:240].replace("\n", " ")
        _log(f"  compaction: gemini exited {rc}; stderr: {snippet}")
        manifest = dict(_EMPTY_MANIFEST)
        manifest["_failure"] = f"gemini exit {rc}"
        return paper_text, manifest

    spans = _extract_json(stdout)
    if spans is None:
        _log(f"  compaction: gemini output not parseable as JSON "
             f"({len(stdout)} chars stdout)")
        manifest = dict(_EMPTY_MANIFEST)
        manifest["_failure"] = "gemini output unparseable"
        return paper_text, manifest

    redacted, manifest, match_failures = _apply_removals(paper_text, spans)

    # If nothing was actually removed (every snippet failed to match), flag
    # it. Otherwise record the metrics + any partial failures.
    if match_failures:
        manifest["match_failures"] = match_failures
    manifest["original_chars"] = len(paper_text)
    manifest["redacted_chars"] = len(redacted)
    manifest["reduction_pct"] = round(
        100 * (1 - len(redacted) / max(len(paper_text), 1)), 1
    )

    return redacted, manifest


# ---------------------------------------------------------------------------
# Human-readable rendering for email + audit display.
# ---------------------------------------------------------------------------

def render_manifest(manifest: dict) -> str:
    """Render a manifest dict as a plain-text bulleted list for the
    decision email's compaction disclosure block."""
    if manifest.get("_failure"):
        return f"(Compaction not applied: {manifest['_failure']}. The panel reviewed the un-stripped manuscript.)"

    lines = []
    if manifest.get("author_names"):
        lines.append(f"  - Author names: {', '.join(manifest['author_names'])}")
    if manifest.get("affiliations"):
        lines.append(f"  - Affiliations: {', '.join(manifest['affiliations'])}")
    if manifest.get("emails"):
        lines.append(f"  - Email addresses: {', '.join(manifest['emails'])}")
    if manifest.get("orcids"):
        lines.append(f"  - ORCID iDs: {', '.join(manifest['orcids'])}")
    if manifest.get("acknowledgments_text"):
        ack_len = len(manifest["acknowledgments_text"])
        lines.append(f"  - Acknowledgments section ({ack_len} characters)")
    if manifest.get("funding_statements"):
        lines.append(f"  - Funding statements: {len(manifest['funding_statements'])} item(s)")
    if manifest.get("references_count") or manifest.get("references_section_chars"):
        lines.append(
            f"  - References list: {manifest.get('references_count', 0)} item(s) "
            f"({manifest.get('references_section_chars', 0)} characters)"
        )

    if not lines:
        lines.append("  (No identifying content detected to remove.)")

    if manifest.get("original_chars") and manifest.get("redacted_chars"):
        lines.append("")
        lines.append(
            f"  Total reduction: {manifest['original_chars']} -> "
            f"{manifest['redacted_chars']} characters "
            f"({manifest.get('reduction_pct', 0)}%)"
        )

    if manifest.get("match_failures"):
        lines.append("")
        lines.append(
            f"  Note: {len(manifest['match_failures'])} identified span(s) "
            "did not match the manuscript verbatim and were not redacted. "
            "Contact help@icsacinstitute.org if you'd like the specifics."
        )

    return "\n".join(lines)


# Panel-facing notice prepended to the redacted manuscript so the panel
# does not penalize the work for "missing" identifying sections or
# reference list. The notice is also archived in the audit log for
# forensic visibility.
PANEL_NOTICE_TEMPLATE = """[BLIND REVIEW PREPROCESSING NOTICE]

Author identifying information (names, affiliations, contact details, ORCID
iDs), the acknowledgments section, funding statements, and the references
list have been removed from this manuscript before review. This is a
standard double-blind preprocessing step intended to keep the panel's
judgment focused on the substance of the work and free from author
identity bias.

Inline citation markers in the body (e.g. [Smith 2024], [1]) are preserved
unchanged. Citation verification against the full reference list was
performed separately upstream and is available to the editorial curator;
the panel should not penalize the manuscript for not displaying its
reference list inline.

The manuscript begins below this notice.

---

"""


def panel_notice() -> str:
    return PANEL_NOTICE_TEMPLATE
