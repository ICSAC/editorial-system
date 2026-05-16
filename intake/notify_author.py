"""Author-facing email notifications for ICSAC paper submissions.

Wraps the top-level email_send primitive with submission-specific templates.
Templates live in intake/templates/ as Markdown with light {{var}}
interpolation; the first line is `Subject: ...`, the rest is the body.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

# Parent-package imports (editorial-system modules live at repo root, one
# level above this intake/ subpackage).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import email_send  # noqa: E402

TEMPLATES_DIR = Path(__file__).parent / "templates"


# Print-optimized CSS for the panel-report and RQC PDFs. Letter trim,
# generous margins, serif body, sans-headings. Tables get borders so
# per-dimension score tables render cleanly. No vendor/model styling
# concerns since the PDFs render the SCRUBBED markdown.
_PDF_CSS = """
@page { size: Letter; margin: 0.85in 0.9in; }
body { font-family: Georgia, "Times New Roman", serif; font-size: 10.5pt; line-height: 1.5; color: #1f1f1f; }
h1, h2, h3, h4 { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color: #111; font-weight: 600; page-break-after: avoid; }
h1 { font-size: 1.5em; border-bottom: 2px solid #888; padding-bottom: 0.3em; margin-top: 0; }
h2 { font-size: 1.18em; margin-top: 1.4em; }
h3 { font-size: 1.04em; margin-top: 1.0em; }
h4 { font-size: 0.98em; margin-top: 0.8em; }
p { margin: 0.55em 0; }
ul, ol { padding-left: 1.4em; margin: 0.55em 0; }
li { margin: 0.18em 0; }
blockquote { border-left: 3px solid #c8c8c8; margin: 0.9em 0; padding: 0.25em 0.9em; color: #555; background: #f7f7f7; font-style: italic; }
code { font-family: "SFMono-Regular", Menlo, Consolas, monospace; background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 0.9em; }
pre { background: #f4f4f4; border: 1px solid #e4e4e4; border-radius: 3px; padding: 0.55em 0.7em; overflow-x: auto; font-size: 0.88em; line-height: 1.4; }
pre code { background: transparent; padding: 0; border-radius: 0; }
table { border-collapse: collapse; margin: 0.7em 0; width: 100%; font-size: 0.95em; }
th, td { border: 1px solid #ccc; padding: 0.35em 0.55em; text-align: left; vertical-align: top; }
th { background: #f0f0f0; font-weight: 600; }
hr { border: none; border-top: 1px solid #ddd; margin: 1.4em 0; }
details > summary { cursor: pointer; font-weight: 600; margin: 0.6em 0; }
"""


def _md_to_pdf_bytes(md_text: str, *, doc_title: str | None = None) -> bytes:
    """Render markdown to a print-quality PDF via WeasyPrint.

    Used for the panel-report and RQC attachments on decision emails. Input
    is the SCRUBBED markdown (vendor/model identifiers and the RQC
    injection_indicators dim already stripped upstream); we don't
    re-validate here — caller owns the redaction gate.
    """
    import markdown as _md_lib
    import weasyprint
    inner = _md_lib.markdown(md_text, extensions=["extra", "sane_lists", "tables"])
    title_html = f"<title>{doc_title}</title>" if doc_title else ""
    full = (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"{title_html}<style>{_PDF_CSS}</style></head>"
        f"<body>{inner}</body></html>"
    )
    return weasyprint.HTML(string=full).write_pdf()


class TemplateUnfilledKeysError(RuntimeError):
    """Raised when a template still contains {{...}} after rendering.

    Hard-fail by design — no silent author-facing breakage. Mirrors
    editorial-system/email_render.TemplateUnfilledKeysError.
    """


def _render(template_name: str, vars: dict) -> tuple[str, str]:
    """Load template, substitute {{vars}}, return (subject, body_md).

    Missing keys leave the literal {{key}} in place; a post-render scan
    catches any survivors and raises so the worker fails loud rather than
    sending mail with an unfilled placeholder.
    """
    raw = (TEMPLATES_DIR / template_name).read_text()

    def sub(match: re.Match) -> str:
        key = match.group(1).strip()
        return str(vars[key]) if key in vars else match.group(0)

    rendered = re.sub(r"\{\{(\w+)\}\}", sub, raw)
    leftover = re.findall(r"\{\{[^}]+\}\}", rendered)
    if leftover:
        raise TemplateUnfilledKeysError(
            f"unfilled keys in {template_name}: {sorted(set(leftover))}"
        )
    lines = rendered.splitlines()
    subject = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_start = i + 1
            break
    body_md = "\n".join(lines[body_start:]).lstrip("\n")
    return subject, body_md


def send_received(to: str, sub_id: str, title: str, author_name: str) -> tuple[bool, str]:
    subject, body = _render("submission_received.md", {
        "icsac_submission_id": sub_id,
        "title": title, "author_name": author_name,
    })
    return email_send.send_email(to_addr=to, subject=subject,
                                 body_md=body, send=True)


def send_intake_failure(*, to: str, sub_id: str, author_name: str,
                        failure_reason: str, remediation: str) -> tuple[bool, str]:
    """Notify author when intake fails before review can begin.

    Used when a deferred DOI fetch fails (Zenodo unreachable, record has no
    PDF, PDF lacks text layer). Does NOT include a panel report because no
    panel ran. Failure reason should be one short sentence; remediation
    should tell the author exactly what to try next."""
    subject, body = _render("submission_intake_failure.md", {
        "icsac_submission_id": sub_id,
        "author_name": author_name,
        "failure_reason": failure_reason,
        "remediation": remediation,
    })
    return email_send.send_email(to_addr=to, subject=subject,
                                 body_md=body, send=True)


def send_published(*, to: str, sub_id: str, title: str, author_name: str,
                   deposit_doi: str, deposit_url: str,
                   publications_url: str) -> tuple[bool, str]:
    """Send the post-publish notification email for a PDF-route accept.

    Fired by the publish_watcher in the editorial-system repo when a curator
    publishes the previously-staged Zenodo draft and the DOI becomes
    permanent. No PDF attachments — the panel report + RQC already shipped
    with the original (pending) accept email; this is the short follow-up
    that closes the loop with the now-real DOI + publications permalink.
    """
    subject, body = _render("submission_published.md", {
        "icsac_submission_id": sub_id,
        "title": title, "author_name": author_name,
        "deposit_doi": deposit_doi,
        "deposit_url": deposit_url,
        "publications_url": publications_url,
    })
    return email_send.send_email(to_addr=to, subject=subject,
                                 body_md=body, send=True)


def send_decision(*, to: str, sub_id: str, title: str, author_name: str,
                  verdict: str, source: str,
                  panel_report_md: str,
                  rqc_md: str = "",
                  source_ref: str = "",
                  deposit_doi: str | None = None,
                  deposit_url: str | None = None,
                  publications_url: str | None = None,
                  tier: int = 1,
                  ) -> tuple[bool, str]:
    """Send the decision email with two PDF attachments (panel report + RQC).

    verdict ∈ {accept, revise, scope_reject}; source ∈ {doi, upload}. Per-source
    template variants live in templates/submission_<verdict>_<source>.md.
    The body references the attachments rather than inlining the panel
    report, keeping the email under ~400 words; the PDFs carry the detail.

    Both `panel_report_md` and `rqc_md` must already be SCRUBBED (the worker's
    _scrubbed_report_pair is the upstream gate). If either is empty the PDF is
    skipped — author still gets the email body, just without that artifact.
    `source_ref` carries the author-supplied DOI on the DOI route; the
    deposit_* fields carry the ICSAC-minted DOI/URL on the upload route once
    the deposit step ships. `publications_url` is the canonical ICSAC indexing
    permalink (https://icsacinstitute.org/publications/<slug>) populated by
    the caller after the registry write succeeds — required for accept emails
    on both routes (the author-facing receipt of editorial endorsement).
    """
    if source not in ("doi", "upload"):
        raise ValueError(f"unknown source: {source!r}")
    # accept+upload routes to the post-deposit copy (references deposit_doi
    # + deposit_url) when the deposit step has succeeded; falls back to the
    # _pending variant — the previous interim copy — when deposit_doi is
    # empty (deposit failed or hasn't run yet for some reason).
    if (verdict, source) == ("accept", "upload") and not deposit_doi:
        template: Optional[str] = "submission_accept_upload_pending.md"
    else:
        template = {
            ("accept", "doi"): "submission_accept_doi.md",
            ("accept", "upload"): "submission_accept_upload.md",
            ("revise", "doi"): "submission_revise_doi.md",
            ("revise", "upload"): "submission_revise_upload.md",
            ("scope_reject", "doi"): "submission_scope_reject_doi.md",
            ("scope_reject", "upload"): "submission_scope_reject_upload.md",
        }.get((verdict, source))
    if not template:
        raise ValueError(f"unknown (verdict, source): {verdict!r}, {source!r}")
    subject, body = _render(template, {
        "icsac_submission_id": sub_id,
        "title": title, "author_name": author_name,
        "source_ref": source_ref or "",
        "deposit_doi": deposit_doi or "",
        "deposit_url": deposit_url or "",
        "publications_url": publications_url or "",
    })

    attachments: list[tuple[str, bytes]] = []
    if panel_report_md.strip():
        attachments.append((
            f"icsac-review-{sub_id}.pdf",
            _md_to_pdf_bytes(panel_report_md, doc_title=f"ICSAC Review — {sub_id}"),
        ))
    if rqc_md.strip():
        attachments.append((
            f"icsac-rqc-{sub_id}.pdf",
            _md_to_pdf_bytes(rqc_md, doc_title=f"ICSAC Review Quality Control — {sub_id}"),
        ))

    # Decision emails (accept/revise/scope_reject) are HIGH-STAKES author-facing
    # correspondence with the panel report + RQC PDFs attached. Per the
    # 2026-05-07 policy change, these go to Gmail Drafts for curator review
    # rather than sending autonomously. submission_received and
    # submission_intake_failure stay auto-send (low-stakes acknowledgements).
    #
    # Tier routing (test mode only — tier=1 is the production default):
    #   tier 1: existing behavior (Gmail Drafts, no subject prefix)
    #   tier 2: write to ~/icsac-submissions/test/_outbox/<sub_id>.eml,
    #          NOT IMAP. Real panel ran but no Gmail interaction.
    #   tier 3: Gmail Drafts WITH `[T3 TEST] ` subject prefix so the
    #          curator can never accidentally send a test draft to a
    #          real author from the Drafts pane.
    if tier == 2:
        from pathlib import Path as _Path
        outbox = _Path.home() / "icsac-submissions" / "test" / "_outbox"
        return email_send.send_email(
            to_addr=to, subject=subject, body_md=body,
            outbox_dir=str(outbox), eml_filename=f"{sub_id}.eml",
            attachments=attachments,
        )
    if tier == 3:
        return email_send.send_email(
            to_addr=to, subject=f"[T3 TEST] {subject}",
            body_md=body, draft=True, attachments=attachments,
        )
    return email_send.send_email(to_addr=to, subject=subject,
                                 body_md=body, draft=True,
                                 attachments=attachments)
