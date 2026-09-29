#!/usr/bin/env python3
"""Regression checks for the reviewer self-identification screen (2026-09-29,
audit item 12f). Offline: no network, no mail, no pings; every write lands in
one temporary directory that is removed at the end, pass or fail. Run from the
repo root:
    .venv/bin/python intake/scripts/test_audit_20260929b.py

Covers: a reviewer or the RQC naming its own model or seat in prose is
rewritten to neutral wording when the sentence survives and dropped when it
cannot; a field that was nothing but self-identification is withheld; text
quoted from the paper and ordinary scientific prose pass untouched; clean text
comes back byte for byte; the grep-gate refuses first-person self-
identification outside quotations; the worker collects the events and flags
the curator on production papers only.
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


REVIEW_MD = """---
title: "Review: A Test Paper on Coupled Oscillators"
doi: "10.0000/test.12f"
record_id: ICSAC-SUB-TEST-12F
review_date: 2026-09-29T12:00:00Z
recommendation: RECOMMEND
disagreement: False
---

## Aggregate scores

| Dimension | Mean | Per-model |
|-----------|------|-----------|
| Scope Alignment | 4.0 | 4, 4 |

## Individual Model Reviews

### panel-a

**Recommendation:** RECOMMEND

**Summary:** As an AI language model, I find the coupling argument sound. The paper quotes a chatbot saying "I am ChatGPT, a language model" and analyses it.

- **Scope Alignment** (4/5): This reviewer (GPT-4o) considers the scope well matched. The oscillator at position 12 of the chain is handled correctly.
- **Methodology** (4/5): I am Claude, and I would add a sensitivity check.

### panel-b

**Recommendation:** RECOMMEND

**Summary:** I am the third reviewer on this panel.

- **Scope Alignment** (4/5): The reviewer in seat 3 overstated the novelty. Claude Shannon's entropy is used correctly.
- **Methodology** (5/5): The author treats the transformer as an AI model of cognition, which is a stated assumption.

---

footer
"""

RQC_MD_TEMPLATE = None  # the RQC builder is exercised on a ParsedRQC directly

tmp = Path(tempfile.mkdtemp(prefix="icsac-audit-20260929b-"))
try:
    import redaction as R

    # ── the screen, one field at a time ─────────────────────────────────────
    s, ev = R.screen_self_identification("As an AI language model, I find the argument sound.", "t")
    check(s == "I find the argument sound." and ev and ev[0]["action"] == "rewritten",
          "leading 'As an AI language model,' is cut and the sentence kept")
    s, ev = R.screen_self_identification("As Claude, I would ask for more data.", "t")
    check(s == "I would ask for more data." and ev[0]["action"] == "rewritten",
          "leading 'As Claude,' is cut and the sentence kept")
    s, ev = R.screen_self_identification("I am Claude and I rate this highly. The method is sound.", "t")
    check(s == "The method is sound." and ev[0]["action"] == "dropped",
          "'I am Claude ...' drops that sentence only")
    s, ev = R.screen_self_identification("This reviewer (GPT-4o) considers the novelty modest.", "t")
    check(s == "This reviewer considers the novelty modest.", "'this reviewer (GPT-4o)' loses the parenthetical")
    s, ev = R.screen_self_identification("Reviewer 3 (Gemini 2.5) was lenient on scope.", "t")
    check(s == "Reviewer 3 was lenient on scope.", "'Reviewer 3 (Gemini 2.5)' loses the parenthetical")
    s, ev = R.screen_self_identification("The reviewer in seat 3 overstated the claim.", "t")
    check(s == "One reviewer overstated the claim.", "'the reviewer in seat 3' becomes 'One reviewer'")
    s, ev = R.screen_self_identification("The panel position 2 reviewer disagreed.", "t")
    check(s == "One reviewer disagreed.", "'panel position 2 reviewer' becomes 'One reviewer'")
    s, ev = R.screen_self_identification("Seat 2 disagreed with the others.", "t")
    check(s == "" and ev[0]["action"] == "dropped", "a sentence built on 'Seat 2' is dropped")
    s, ev = R.screen_self_identification("I'm the second reviewer, so I defer.", "t")
    check(s == "" and ev[0]["action"] == "dropped", "'I'm the second reviewer' is dropped")
    s, ev = R.screen_self_identification("I cannot, as an AI assistant, verify the data.", "t")
    check(s == "" and ev[0]["action"] == "dropped", "mid-sentence 'as an AI assistant' with first person is dropped")

    # ── what must pass untouched ────────────────────────────────────────────
    for txt, why in (
        ('The paper quotes the model output "I am ChatGPT, a language model" verbatim.', "a quotation from the paper"),
        ("The paper quotes the output “I am Claude” in its appendix.", "a curly-quoted quotation"),
        ("The paper studies GPT-2 activations at position 12 of the sequence.", "GPT-2 as a subject, sequence position"),
        ("Claude Shannon's entropy is applied correctly.", "Claude Shannon"),
        ("The author treats the transformer as an AI model of cognition.", "'as an AI model' with no first person"),
        ("This position is untenable without a control.", "'this position' as an argument"),
        ("The authors used Gemini to draft the abstract, as disclosed.", "an author AI-use disclosure"),
        ("Multi-sentence text.  With two spaces.\nAnd a newline.", "clean text with odd whitespace"),
    ):
        out, ev = R.screen_self_identification(txt, "t")
        check(out == txt and ev == [], f"untouched: {why}")

    # ── the panel builder ───────────────────────────────────────────────────
    p = tmp / "ICSAC-SUB-TEST-12F_a-test-paper.md"
    p.write_text(REVIEW_MD)
    parsed = R.parse_review_file(str(p))
    md = R.build_public_markdown(parsed)
    check("As an AI language model" not in md and "I find the coupling argument sound." in md,
          "panel: summary rewritten in the public record")
    check("I am Claude" not in md and "GPT-4o" not in md, "panel: model self-names gone")
    check("&quot;I am ChatGPT, a language model&quot;" in md, "panel: the paper's quotation kept")
    check("position 12 of the chain" in md and "Claude Shannon" in md, "panel: subject matter kept")
    check("I am the third reviewer" not in md and R.SELF_ID_WITHHELD in md,
          "panel: a summary that was only self-identification is withheld")
    check("One reviewer overstated the novelty." in md, "panel: seat reference rewritten")
    check("as an AI model of cognition" in md, "panel: 'as an AI model' as subject kept")
    acts = sorted(e["action"] for e in parsed.self_id_events)
    check(acts.count("dropped") == 2 and acts.count("rewritten") == 3,
          f"panel: events recorded (got {acts})")
    rep = R.assert_clean(md)
    html = R.render_public_html(md)
    R.assert_clean(html)
    check(rep.clean, "panel: the screened record passes the grep-gate, markdown and html")
    md2 = R.build_public_markdown(parsed)
    check(md2 == md and len(parsed.self_id_events) == 5, "panel: a rebuild is identical and does not double the events")

    # ── the RQC builder ─────────────────────────────────────────────────────
    rqc = R.ParsedRQC(
        record_id="ICSAC-SUB-TEST-12F", title="A Test Paper", doi="10.0000/test.12f",
        audit_date="2026-09-29T12:05:00Z", flag=False, summary="",
        overall_concerns=[
            "As Gemini, I note the panel was lenient on novelty.",
            "I am the fifth reviewer and I disagree.",
            "The panel under-weighted the missing control.",
        ],
        slots=[{"reviewer": "x", "errored": False, "dimensions": [
            ("Rubric Adherence", "4/5", "I am Claude. Rubric followed."),
            ("Internal Consistency", "4/5", "I'm the first reviewer."),
            ("Specificity", "5/5", "Cites the equations directly."),
            ("Tone", "5/5", "Neutral and measured."),
        ]}],
    )
    rmd = R.build_public_rqc_markdown(rqc)
    check("I note the panel was lenient on novelty." in rmd and "As Gemini" not in rmd, "rqc: note rewritten")
    check("fifth reviewer" not in rmd and "The panel under-weighted the missing control." in rmd,
          "rqc: a self-identifying note is dropped, the others kept")
    check("(4/5): Rubric followed." in rmd and "I am Claude" not in rmd, "rqc: justification keeps its surviving sentence")
    check(f"(4/5): {R.SELF_ID_WITHHELD}" in rmd, "rqc: a justification that was only self-identification is withheld")
    check(len(rqc.self_id_events) == 4, f"rqc: events recorded (got {len(rqc.self_id_events)})")
    R.assert_rqc_clean(rmd)
    R.assert_rqc_clean(R.render_public_html(rmd))
    check(True, "rqc: the screened record passes the grep-gate, markdown and html")

    # ── the grep-gate backstop ──────────────────────────────────────────────
    bad = R.scan("Structural text. I am Claude, the reviewer.")
    check(not bad.clean and any("regex" in t for t, _ in bad.fatal_hits), "gate: 'I am Claude' outside quotes is fatal")
    bad = R.scan("Note: I'm an AI language model.")
    check(not bad.clean, "gate: 'I'm an AI language model' is fatal")
    ok_ = R.scan('The paper quotes "I am Gemini" in its appendix.')
    check(ok_.clean, "gate: a quoted self-report from the paper passes")
    ok_ = R.scan("<p>The paper quotes &quot;I am Gemini&quot; in its appendix.</p>")
    check(ok_.clean, "gate: an html-escaped quotation passes")

    # ── the worker ──────────────────────────────────────────────────────────
    from intake import submission_worker as sw
    sw.TEST_REVIEWS_DIR = tmp
    events: list = []
    panel_md, rqc_md = sw._scrubbed_report_pair("ICSAC-SUB-TEST-12F", "A Test Paper",
                                                tier=2, screen_events=events)
    check(bool(panel_md) and "I am Claude" not in panel_md and len(events) == 5,
          f"worker: the PDF markdown is screened and the events come back (got {len(events)})")
    p2, _ = sw._scrubbed_report_pair("ICSAC-SUB-TEST-12F", "A Test Paper", tier=2)
    check(p2 == panel_md, "worker: callers that pass no list get the same text")
    src = inspect.getsource(sw._email_decision)
    block = src.split("if screen_events:", 1)[1].split("ok, _msg = notify_author.send_decision", 1)[0] \
        if "if screen_events:" in src else ""
    check("screen_events=screen_events" in src and "if tier == 1:" in block and "send_to_curator" in block,
          "worker: the decision draft flags the curator on production papers only")
    fmt = R.format_self_id_events(parsed.self_id_events)
    check(fmt.startswith("2 sentence(s) dropped, 3 rewritten"), "curator summary counts the events")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
