"""SMTP delivery for ICSAC author correspondence.

Sends HTML (multipart/alternative) email through Gmail SMTP with the ICSAC
logo inlined as a CID attachment. From header uses info@icsacinstitute.org as a Send-As alias
over a backing SMTP mailbox.

Defaults to dry-run mode for safety. Pass send=True to actually deliver.
"""

import os
import re
import smtplib
import ssl
import time
import imaplib
from email.message import EmailMessage

import markdown

import config


LOGO_CID = "icsac-logo"
LOGO_PATH = os.path.join(config.BASE_DIR, "assets", "icsac-logo.png")

HTML_WRAPPER = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #222; max-width: 640px; margin: 0 auto; padding: 24px; background: #fff; }}
h1, h2, h3 {{ color: #111; margin-top: 1.6em; margin-bottom: 0.6em; font-weight: 600; }}
h2 {{ font-size: 1.15em; }}
p {{ margin: 0.8em 0; }}
a {{ color: #2a6cd4; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
blockquote {{ border-left: 3px solid #c8c8c8; margin: 1em 0; padding: 0.3em 1em; color: #555; background: #f7f7f7; font-style: italic; }}
ul {{ padding-left: 1.4em; }}
li {{ margin: 0.3em 0; }}
hr {{ border: none; border-top: 1px solid #e4e4e4; margin: 2em 0 1em; }}
.logo {{ text-align: center; margin-bottom: 28px; }}
.logo img {{ max-width: 320px; height: auto; }}
</style>
</head>
<body>
<div class="logo"><img src="cid:{cid}" alt="ICSAC"></div>
{body}
</body>
</html>
"""


def _markdown_to_plaintext(md: str) -> str:
    """Strip markdown syntax for the plain-text alternative."""
    md = re.sub(r'^#{1,6}\s+', '', md, flags=re.MULTILINE)
    md = re.sub(r'\*\*([^*]+)\*\*', r'\1', md)
    md = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'\1', md)
    md = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', md)
    md = re.sub(r'^>\s?', '', md, flags=re.MULTILINE)
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip() + "\n"


def _markdown_to_html(md: str) -> str:
    """Render markdown body to HTML with a branded wrapper and inline logo CID."""
    inner = markdown.markdown(md, extensions=["extra", "sane_lists"])
    return HTML_WRAPPER.format(cid=LOGO_CID, body=inner)


def extract_subject(rendered_template: str) -> str:
    """Pull the Subject line out of a rendered email template."""
    for line in rendered_template.splitlines():
        if line.lower().startswith("subject:"):
            return line.split(":", 1)[1].strip()
    return "ICSAC Community"


def extract_body(rendered_template: str) -> str:
    """Strip the template header (title, subject, ---) before the first '---' separator."""
    parts = rendered_template.split("\n---\n", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return rendered_template.strip()


def send_email(to_addr: str, subject: str, body_md: str,
               from_name: str = "ICSAC",
               send: bool = False,
               draft: bool = False,
               attachments: list[tuple[str, bytes]] | None = None,
               outbox_dir: str | None = None,
               eml_filename: str | None = None,
               ) -> tuple[bool, str]:
    """Send a multipart email (plain-text + HTML, inline logo, optional attachments).

    `body_md` is the markdown body (what lives below the template's --- separator).
    `attachments` is an optional list of (filename, raw_bytes) pairs; PDFs go out
    as application/pdf, anything else as application/octet-stream. EmailMessage
    promotes the multipart structure to multipart/mixed automatically when
    attachments are appended on top of the existing alternative+related layout.

    Four delivery modes (mutually exclusive):
      send=False, draft=False  →  DRY RUN (default; safe)
      send=True                →  SMTP send via Gmail
      draft=True               →  IMAP APPEND to Gmail Drafts (operator
                                  manually reviews and sends from Gmail UI)
      outbox_dir=<path>        →  Write rendered MIME to <outbox_dir>/<name>.eml.
                                  No SMTP, no IMAP. Used by Tier 2 test-pipeline
                                  runs so a real panel exercise produces a real
                                  on-disk decision artifact without touching
                                  Gmail or the author. `eml_filename` overrides
                                  the on-disk filename; default derives from
                                  the subject slug.
    """
    modes = sum(int(bool(x)) for x in (send, draft, outbox_dir))
    if modes > 1:
        return (False, "send, draft, and outbox_dir are mutually exclusive")
    smtp_host = getattr(config, "SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(getattr(config, "SMTP_PORT", 465))
    smtp_user = getattr(config, "SMTP_USER", "")
    smtp_pass = getattr(config, "SMTP_PASSWORD", "")
    from_addr = getattr(config, "FROM_EMAIL", "info@icsacinstitute.org")
    reply_to = getattr(config, "REPLY_TO_EMAIL", from_addr)

    if not (smtp_user and smtp_pass) and not outbox_dir:
        # outbox_dir mode is purely on-disk; no Gmail creds required.
        # All other modes (DRY RUN, send, draft) need the SMTP creds
        # configured because the From/Reply-To headers are derived from
        # them and IMAP login uses the same pair.
        return (False, "SMTP_USER or SMTP_PASSWORD not configured")
    if not to_addr or "@" not in to_addr:
        return (False, f"invalid recipient: {to_addr!r}")

    plain = _markdown_to_plaintext(body_md)
    html = _markdown_to_html(body_md)

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Reply-To"] = reply_to
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    try:
        with open(LOGO_PATH, "rb") as f:
            logo_data = f.read()
        msg.get_payload()[1].add_related(
            logo_data, maintype="image", subtype="png", cid=f"<{LOGO_CID}>"
        )
    except FileNotFoundError:
        return (False, f"logo asset missing: {LOGO_PATH}")

    for filename, data in (attachments or []):
        subtype = "pdf" if filename.lower().endswith(".pdf") else "octet-stream"
        msg.add_attachment(
            data, maintype="application", subtype=subtype, filename=filename,
        )

    if outbox_dir:
        # Tier-2 test-pipeline target: write the rendered MIME message
        # to disk and return. No SMTP, no IMAP, no Gmail interaction at
        # all. The .eml file is the operator's audit trail that the
        # Tier-2 panel ran to completion and the decision email
        # rendered cleanly without burning a real send to the author.
        try:
            outdir = os.path.abspath(os.path.expanduser(str(outbox_dir)))
            os.makedirs(outdir, exist_ok=True)
            if eml_filename:
                fname = eml_filename
            else:
                slug = re.sub(r"[^A-Za-z0-9]+", "-", subject).strip("-")[:60].lower() or "message"
                fname = f"{slug}.eml"
            if not fname.lower().endswith(".eml"):
                fname += ".eml"
            target = os.path.join(outdir, fname)
            with open(target, "wb") as f:
                f.write(msg.as_bytes())
            return (True, f"wrote outbox eml: {target}")
        except Exception as e:
            return (False, f"outbox write failed: {type(e).__name__}: {e}")

    if draft:
        # IMAP APPEND to Gmail Drafts. Operator opens Gmail, reviews, sends.
        # Same MIME message that SMTP would deliver — when the operator opens the
        # draft, Gmail's From: dropdown still lets him pick the alias.
        imap_host = getattr(config, "IMAP_HOST", "imap.gmail.com")
        imap_port = int(getattr(config, "IMAP_PORT", 993))
        imap_user = getattr(config, "IMAP_USER", smtp_user)
        imap_pass = getattr(config, "IMAP_PASSWORD", smtp_pass)
        drafts_folder = getattr(config, "IMAP_DRAFTS_FOLDER", "[Gmail]/Drafts")
        try:
            with imaplib.IMAP4_SSL(imap_host, imap_port) as imap:
                imap.login(imap_user, imap_pass)
                raw = msg.as_bytes()
                date = imaplib.Time2Internaldate(time.time())
                typ, data = imap.append(drafts_folder, "\\Draft", date, raw)
                if typ != "OK":
                    return (False, f"IMAP APPEND returned {typ}: {data!r}")
            return (True, f"draft saved to {drafts_folder} for {to_addr}")
        except imaplib.IMAP4.error as e:
            return (False, f"IMAP error (check Gmail app password + IMAP enabled): {e}")
        except Exception as e:
            return (False, f"draft save failed: {type(e).__name__}: {e}")

    if not send:
        return (True, f"DRY RUN: would send to {to_addr!r} via {smtp_host}:{smtp_port} as {smtp_user} "
                      f"(From: {from_name} <{from_addr}>, subject: {subject!r})")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return (True, f"sent to {to_addr}")
    except smtplib.SMTPAuthenticationError as e:
        return (False, f"SMTP auth failed (check Gmail app password): {e}")
    except Exception as e:
        return (False, f"SMTP error: {type(e).__name__}: {e}")


def send_accept_email(to_addr: str, rendered_template: str, send: bool = False) -> tuple[bool, str]:
    return send_email(
        to_addr=to_addr,
        subject=extract_subject(rendered_template),
        body_md=extract_body(rendered_template),
        send=send,
    )


def send_revise_and_resubmit_email(to_addr: str, rendered_template: str, send: bool = False) -> tuple[bool, str]:
    return send_email(
        to_addr=to_addr,
        subject=extract_subject(rendered_template),
        body_md=extract_body(rendered_template),
        send=send,
    )


def send_scope_reject_email(to_addr: str, rendered_template: str, send: bool = False) -> tuple[bool, str]:
    return send_email(
        to_addr=to_addr,
        subject=extract_subject(rendered_template),
        body_md=extract_body(rendered_template),
        send=send,
    )


def send_invite_email(to_addr: str, rendered_template: str, send: bool = False) -> tuple[bool, str]:
    return send_email(
        to_addr=to_addr,
        subject=extract_subject(rendered_template),
        body_md=extract_body(rendered_template),
        send=send,
    )
