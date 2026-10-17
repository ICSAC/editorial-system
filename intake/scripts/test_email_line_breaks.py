#!/usr/bin/env python3
"""The HTML part of an email shows the templates' header block and sign-off
one item per line, as the plain-text part does; paragraphs stay paragraphs.
Every template, both template sets. Offline, no files written. Run from the
repo root with the venv:
    .venv/bin/python intake/scripts/test_email_line_breaks.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import email_send  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


html = email_send._markdown_to_html(
    "The review is complete.\n\n"
    "**Submission ID:** ICSAC-SUB-00002\n**Revision of:** ICSAC-SUB-00001\n**Title:** T\n\n"
    "Next paragraph.\n\n— ICSAC\nInstitute for Complexity Science and Advanced Computing")
check("<strong>Submission ID:</strong> ICSAC-SUB-00002<br />\n<strong>Revision of:</strong> ICSAC-SUB-00001<br />\n"
      "<strong>Title:</strong> T</p>" in html, "the header block is one item per line")
check("— ICSAC<br />\nInstitute for Complexity" in html, "the sign-off is one item per line")
check("<p>The review is complete.</p>" in html and "<p>Next paragraph.</p>" in html,
      "paragraphs stay separate paragraphs")
check("&lt;" in email_send._markdown_to_html("a <b>x</b>"), "a substituted < is still text")

# Every soft line break in every template comes out as a <br />.
dirs = [ROOT / "intake" / "templates", ROOT / "templates"]
n_templates = n_breaks = 0
for d in dirs:
    for f in sorted(d.glob("*.md")):
        lines = [ln for ln in f.read_text().splitlines() if not ln.lower().startswith("subject:")]
        body = "\n".join(lines)
        out = email_send._markdown_to_html(body)
        n_templates += 1
        for a, b in zip(lines, lines[1:]):
            if not a.strip() or not b.strip():
                continue
            if re.match(r"\s*([-*] |\d+\. |#|>|\||---)", a) or re.match(r"\s*([-*] |\d+\. |#|\||---)", b):
                continue
            n_breaks += 1
            tail = re.sub(r"\{\{\w+\}\}|\*\*|\[|\]\([^)]*\)|\]", "", a).strip()[-12:]
            check(bool(tail) and re.search(re.escape(tail) + r"[^\n]*<br />", out) is not None,
                  f"{d.name}/{f.name}: the break after ...{tail!r} is kept")
check(n_templates >= 20 and n_breaks >= 30, f"looked at {n_templates} templates, {n_breaks} line breaks")

sys.exit(1 if failures else 0)
