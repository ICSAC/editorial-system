"""Direct ebook sale — Stripe verification + purchase-confirmation email.

Additive to the intake server and deliberately isolated from the paper-
submission flow. Two entry points, called from intake_server routes:

  verify_ebook_session(session_id) -> (http_status, body_dict)
      Confirms a Checkout Session is paid AND is for the ebook product.
      Status codes the CF /api/ebook-download proxy understands:
        200 paid · 402 unpaid · 403 wrong product · 404 not found.

  handle_ebook_webhook(payload, sig_header) -> (http_status, body_dict)
      Validates the Stripe webhook signature and, on a paid
      checkout.session.completed for the ebook, emails the buyer their
      durable /books/download link (re-verifiable any time).

Reuses the existing STRIPE_RESTRICTED_KEY (already on the intake server)
and the repo-root email_send mailer. New env vars:
  EBOOK_PRODUCT_ID             prod_… of the ebook product
  STRIPE_EBOOK_WEBHOOK_SECRET  whsec_… of the checkout webhook endpoint
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time

import httpx

SESSION_ID_RE = re.compile(r"^cs_(live|test)_[A-Za-z0-9]+$")
DOWNLOAD_PAGE = "https://icsacinstitute.org/books/download"
WEBHOOK_TOLERANCE_SECONDS = 300
# Filesystem set of session_ids already emailed, so a buyer reloading the
# download page doesn't get the confirmation email more than once.
SENT_DIR = os.path.expanduser("~/icsac-ebook-sent")


def _stripe_key() -> str:
    return os.environ.get("STRIPE_RESTRICTED_KEY", "").strip()


def _product_id() -> str:
    return os.environ.get("EBOOK_PRODUCT_ID", "").strip()


def _webhook_secret() -> str:
    return os.environ.get("STRIPE_EBOOK_WEBHOOK_SECRET", "").strip()


def _retrieve_session(session_id: str) -> httpx.Response:
    """Fetch a Checkout Session expanded with its line items + their price."""
    url = "https://api.stripe.com/v1/checkout/sessions/" + session_id
    params = [
        ("expand[]", "line_items"),
        ("expand[]", "line_items.data.price"),
    ]
    headers = {"Authorization": "Bearer " + _stripe_key()}
    with httpx.Client(timeout=10.0) as client:
        return client.get(url, params=params, headers=headers)


def _is_paid_ebook(session: dict) -> tuple[bool, str]:
    """Classify a retrieved session. Returns (ok, reason)."""
    if session.get("mode") != "payment":
        return (False, "not_payment_mode")
    if session.get("payment_status") != "paid":
        return (False, "unpaid")
    items = (session.get("line_items") or {}).get("data") or []
    pid = _product_id()
    for it in items:
        price = it.get("price") or {}
        # price.product is a string id (or, if further expanded, a dict).
        product = price.get("product")
        if isinstance(product, dict):
            product = product.get("id")
        if product == pid:
            return (True, "ok")
    return (False, "wrong_product")


def verify_ebook_session(session_id: str) -> tuple[int, dict]:
    if not _product_id():
        return (500, {"error": "EBOOK_PRODUCT_ID not configured"})
    if not _stripe_key():
        return (500, {"error": "STRIPE_RESTRICTED_KEY not configured"})
    if not SESSION_ID_RE.match(session_id or ""):
        return (400, {"error": "bad_session_id"})

    try:
        resp = _retrieve_session(session_id)
    except httpx.HTTPError:
        return (502, {"error": "stripe_unreachable"})

    if resp.status_code == 404:
        return (404, {"paid": False, "reason": "not_found"})
    if resp.status_code != 200:
        return (502, {"error": f"stripe_{resp.status_code}"})

    session = resp.json()
    ok, reason = _is_paid_ebook(session)
    if ok:
        # Email the durable link on first confirmed view (deduped, non-blocking).
        details = session.get("customer_details") or {}
        email = details.get("email") or session.get("customer_email") or ""
        send_download_email_once(session_id, email)
        return (200, {"paid": True})
    if reason in ("unpaid", "not_payment_mode"):
        return (402, {"paid": False, "reason": reason})
    return (403, {"paid": False, "reason": reason})


def _verify_webhook_sig(payload: bytes, sig_header: str) -> bool:
    """Stripe signature scheme: header is 't=...,v1=...'; signed payload is
    '<t>.<raw body>' HMAC-SHA256'd with the endpoint secret."""
    secret = _webhook_secret()
    if not secret or not sig_header:
        return False
    parts = {}
    for chunk in sig_header.split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts.setdefault(k.strip(), v.strip())
    t = parts.get("t")
    v1 = parts.get("v1")
    if not t or not v1:
        return False
    try:
        ts = int(t)
    except ValueError:
        return False
    if abs(time.time() - ts) > WEBHOOK_TOLERANCE_SECONDS:
        return False
    signed = t.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def _download_email_body(link: str) -> str:
    return f"""## Thank you for your purchase

Your copy of **Foundations of the Existence Threshold: The Scholarly
Collection** is ready to download in both formats — reflowable **EPUB**
(Kindle, Apple Books, Kobo, phone/tablet apps) and the full typeset
**PDF** (with cover, for desktop reading and printing).

[**Download your ebook →**]({link})

This link is yours to keep. Open it any time to download either format
again — it re-issues fresh download links on each visit.

If anything doesn't work, just reply to this email or write to
info@icsacinstitute.org and we'll sort it out.

— The Institute for Complexity Science and Advanced Computing
"""


def _send_download_email(to_addr: str, session_id: str) -> tuple[bool, str]:
    import email_send  # repo-root mailer (on sys.path at runtime)

    link = f"{DOWNLOAD_PAGE}?session_id={session_id}"
    return email_send.send_email(
        to_addr=to_addr,
        subject="Your ebook — Foundations of the Existence Threshold",
        body_md=_download_email_body(link),
        from_name="ICSAC Publications",
        send=True,
    )


def _claim_send(session_id: str) -> str | None:
    """Atomically claim the right to email this session_id exactly once.
    Returns the marker path if we claimed it, or None if already claimed."""
    try:
        os.makedirs(SENT_DIR, exist_ok=True)
        path = os.path.join(SENT_DIR, session_id)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return path
    except FileExistsError:
        return None
    except OSError:
        return None


def send_download_email_once(session_id: str, email: str) -> None:
    """Fire-and-forget: email the durable download link once per session.
    Non-blocking so the verify response (and the buyer's download page) isn't
    held up by SMTP. If the send fails, the claim is released so a later page
    visit retries."""
    if not email or not SESSION_ID_RE.match(session_id or ""):
        return
    path = _claim_send(session_id)
    if not path:
        return  # already sent (or claimed by a concurrent request)

    def _worker() -> None:
        try:
            ok, _msg = _send_download_email(email, session_id)
        except Exception:
            ok = False
        if not ok:
            try:
                os.unlink(path)  # release claim so a later visit can retry
            except OSError:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def handle_ebook_webhook(payload: bytes, sig_header: str) -> tuple[int, dict]:
    if not _verify_webhook_sig(payload, sig_header):
        return (400, {"error": "bad_signature"})
    try:
        event = json.loads(payload)
    except Exception:
        return (400, {"error": "bad_json"})

    if event.get("type") != "checkout.session.completed":
        return (200, {"ignored": event.get("type")})

    session = (event.get("data") or {}).get("object") or {}
    session_id = session.get("id")
    if not session_id or session.get("payment_status") != "paid":
        return (200, {"ignored": "unpaid_or_no_id"})

    # The webhook payload doesn't expand line_items, so re-retrieve to confirm
    # the purchase is actually our ebook product before emailing.
    try:
        resp = _retrieve_session(session_id)
        full = resp.json() if resp.status_code == 200 else {}
    except httpx.HTTPError:
        full = {}
    ok, reason = _is_paid_ebook(full) if full else (False, "lookup_failed")
    if not ok:
        return (200, {"ignored": reason})

    details = session.get("customer_details") or {}
    email = details.get("email") or session.get("customer_email")
    if not email:
        return (200, {"ignored": "no_email"})

    sent, msg = _send_download_email(email, session_id)
    if not sent:
        # 500 so Stripe retries the webhook rather than dropping the email.
        return (500, {"error": "email_failed", "detail": msg})
    return (200, {"emailed": True})
