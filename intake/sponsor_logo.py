"""ICSAC sponsor-logo intake — handles the after-checkout logo upload.

Lives next to intake_server.py and gets wired in via a single import +
route registration there. Kept in its own module so the submission
intake stays focused; this module owns the website-repo side effects
(file write, supporters.yaml mutation, git commit + push).

The Stripe Checkout Session ID is the auth token: anyone with a valid
paid Sponsor session can upload a logo for that session. Stripe is
queried with a restricted read-only key to verify:
  - session.payment_status == 'paid'
  - session.mode == 'subscription'
  - the subscription's items contain the Sponsor product

Display name + website URL come from the session's custom_fields
(collected at checkout), not the form — sponsors don't retype.

Filename for the uploaded logo: first 8 chars of sha256(session_id) +
canonical extension by mime sniff (PNG/JPEG/SVG/WebP). The session-id
hash is opaque, collision-free in practice, and stable for re-uploads.

Side effects per successful upload:
  1. Logo written to icsacinstitute.org/public/supporter-logos/<8hex>.<ext>
  2. supporters.yaml updated (record keyed by stripe_sub id; logo_pending
     cleared, logo_path set, display_name + website_url filled).
  3. `git commit && git push origin main` from the website repo.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import HTTPException, Request, UploadFile

# Sponsor product (live mode). Locked to ICSAC Sponsor; receiver rejects
# sessions whose subscription items don't reference this product.
SPONSOR_PRODUCT_ID = "prod_UXiXkUln7XbrQz"

# Website repo on this host. The intake server runs as `orangepi`; the
# repo's SSH remote uses the github.com-3rwt alias which already has
# the dedicated push key.
WEBSITE_REPO = Path.home() / "Desktop" / "icsac" / "icsacinstitute.org"
LOGO_DIR = WEBSITE_REPO / "public" / "supporter-logos"
SUPPORTERS_YAML = WEBSITE_REPO / "src" / "data" / "supporters.yaml"

# Multipart upload cap. CF Pages edge already enforces ~5 MB; this is
# the belt-and-suspenders check after the body parse.
MAX_LOGO_BYTES = 5 * 1024 * 1024

# Mime → canonical extension map. Only these types are accepted; the
# server sniffs magic bytes so a renamed .exe can't get through.
ALLOWED_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}

SESSION_ID_RE = re.compile(r"^cs_(live|test)_[A-Za-z0-9]+$")


def _stripe_key() -> str:
    key = os.environ.get("STRIPE_RESTRICTED_KEY", "").strip()
    if not key:
        raise HTTPException(500, "STRIPE_RESTRICTED_KEY not configured")
    return key


def _sniff_mime(head: bytes) -> str | None:
    """Identify mime by magic bytes (not by client-supplied content-type)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    # SVG: XML declaration or <svg ...> root. Be tolerant of BOM/whitespace.
    stripped = head.lstrip().lower()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<svg"):
        return "image/svg+xml"
    return None


def _retrieve_checkout_session(session_id: str) -> dict[str, Any]:
    """Fetch a Checkout Session expanded with its subscription."""
    url = "https://api.stripe.com/v1/checkout/sessions/" + session_id
    params = [
        ("expand[]", "subscription"),
        ("expand[]", "subscription.items.data.price"),
    ]
    headers = {"Authorization": "Bearer " + _stripe_key()}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Stripe unreachable: {e}") from e

    if resp.status_code == 404:
        raise HTTPException(404, "checkout session not found")
    if resp.status_code != 200:
        raise HTTPException(
            502, f"Stripe error {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


def _verify_sponsor_session(session: dict[str, Any]) -> dict[str, Any]:
    """Confirm the session is paid + is a Sponsor sub. Extract canonical fields.

    Returns dict with display_name, website_url, stripe_sub, current_period_end.
    """
    if session.get("mode") != "subscription":
        raise HTTPException(400, "session is not a subscription checkout")
    if session.get("payment_status") != "paid":
        raise HTTPException(400, "session payment is not complete")

    subscription = session.get("subscription")
    if not isinstance(subscription, dict):
        raise HTTPException(400, "no subscription on session")

    items = (subscription.get("items") or {}).get("data") or []
    matched = False
    for it in items:
        price = it.get("price") or {}
        if price.get("product") == SPONSOR_PRODUCT_ID:
            matched = True
            break
    if not matched:
        raise HTTPException(
            403,
            "session's subscription is not a Sponsor tier — logo upload only "
            "available for ICSAC Sponsor subscribers.",
        )

    # custom_fields is a list of {key, label, type, text:{value}, ...}
    display_name = ""
    website_url = ""
    for cf in session.get("custom_fields") or []:
        key = cf.get("key", "")
        text = (cf.get("text") or {}).get("value", "") or ""
        if key == "display_name":
            display_name = text.strip()
        elif key == "website_url":
            website_url = text.strip()

    if not display_name:
        raise HTTPException(400, "session is missing display_name custom field")
    if not website_url:
        raise HTTPException(400, "session is missing website_url custom field")

    # Light website URL normalisation — Stripe lets users type bare hostnames.
    if not re.match(r"^https?://", website_url, re.IGNORECASE):
        website_url = "https://" + website_url

    # current_period_end migrated from subscription top-level to
    # subscription.items.data[i].current_period_end in recent Stripe API
    # versions (per-item billing cycles). Read the item first; fall back
    # to the legacy top-level field for older API versions.
    period_end = None
    for it in items:
        cpe = it.get("current_period_end")
        if isinstance(cpe, int):
            period_end = cpe
            break
    if period_end is None:
        cpe = subscription.get("current_period_end")
        if isinstance(cpe, int):
            period_end = cpe
    valid_through = ""
    if isinstance(period_end, int):
        valid_through = datetime.fromtimestamp(period_end, tz=timezone.utc).date().isoformat()

    return {
        "display_name": display_name,
        "website_url": website_url,
        "stripe_sub": subscription.get("id", ""),
        "valid_through": valid_through,
    }


def _load_supporters() -> dict[str, Any]:
    if not SUPPORTERS_YAML.exists():
        return {"supporters": []}
    with SUPPORTERS_YAML.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        loaded = {}
    if not isinstance(loaded.get("supporters"), list):
        loaded["supporters"] = []
    return loaded


def _write_supporters(data: dict[str, Any]) -> None:
    """Atomic write — temp file + rename so a crash mid-write can't corrupt."""
    SUPPORTERS_YAML.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUPPORTERS_YAML.with_suffix(SUPPORTERS_YAML.suffix + ".tmp")
    header = (
        "# ICSAC Supporters Registry\n"
        "# Written by sponsor-logo intake + Stripe poll job.\n"
        "# Schema: see commit history of this file.\n\n"
    )
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    with tmp.open("w", encoding="utf-8") as f:
        f.write(header + body)
    tmp.replace(SUPPORTERS_YAML)


def _upsert_sponsor(
    supporters_file: dict[str, Any],
    *,
    stripe_sub: str,
    display_name: str,
    website_url: str,
    logo_path: str,
    valid_through: str,
) -> None:
    """Append or update the sponsor record keyed by stripe_sub."""
    records = supporters_file["supporters"]
    today = datetime.now(tz=timezone.utc).date().isoformat()
    existing = next(
        (r for r in records if isinstance(r, dict) and r.get("stripe_sub") == stripe_sub),
        None,
    )
    if existing is None:
        records.append({
            "display_name": display_name,
            "tier": "sponsor",
            "joined": today,
            "valid_through": valid_through,
            "status": "active",
            "stripe_sub": stripe_sub,
            "website_url": website_url,
            "logo_path": logo_path,
        })
    else:
        existing["display_name"] = display_name
        existing["tier"] = "sponsor"
        existing["status"] = "active"
        existing["website_url"] = website_url
        existing["logo_path"] = logo_path
        if valid_through:
            existing["valid_through"] = valid_through
        existing.pop("logo_pending", None)
        existing["stripe_sub"] = stripe_sub


def _git(*args: str) -> None:
    """Run a git command in the website repo. Raise 500 with a clean message
    on failure so the API caller gets a real error instead of a stack trace."""
    proc = subprocess.run(
        ["git", *args],
        cwd=WEBSITE_REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = " | ".join(tail[-3:]) if tail else "git command failed"
        raise HTTPException(500, f"git {args[0]} failed: {msg}")


def _git_commit_push(logo_path_rel: str, display_name: str) -> None:
    """Commit supporters.yaml + the new logo, then push to origin/main."""
    # Pull first so the push can fast-forward — the website repo is the
    # source of truth and may have unrelated commits from elsewhere.
    _git("pull", "--rebase", "--autostash", "origin", "main")
    _git("add", "src/data/supporters.yaml", logo_path_rel)
    # If nothing changed (re-upload with identical bytes + same record),
    # commit will fail; treat that as success.
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=WEBSITE_REPO,
        check=False,
    )
    if status.returncode == 0:
        return  # nothing staged; bail without commit/push
    msg = f"sponsors: add {display_name} logo"
    _git("-c", "user.email=info@icsacinstitute.org",
         "-c", "user.name=ICSAC",
         "commit", "-m", msg)
    _git("push", "origin", "main")


def prefill_for_session(session_id: str) -> dict[str, Any]:
    """Return display_name + website_url for a paid Sponsor session.

    Returns {"valid": False, ...} for any failure (bad session, not paid,
    not a sponsor tier, etc.) — the prefill GET endpoint should never
    leak Stripe-side errors back to the browser since they're not
    actionable for the user.
    """
    if not SESSION_ID_RE.match(session_id or ""):
        return {"valid": False, "reason": "bad_session_id"}
    try:
        session = _retrieve_checkout_session(session_id)
        info = _verify_sponsor_session(session)
    except HTTPException:
        return {"valid": False}
    except Exception:  # noqa: BLE001 — never leak prefill internals to client
        return {"valid": False}
    return {
        "valid": True,
        "display_name": info["display_name"],
        "website_url": info["website_url"],
    }


async def handle_sponsor_logo(
    request: Request,
    session_id: str,
    logo: UploadFile,
    raw_body: bytes,
    form_display_name: str = "",
    form_website_url: str = "",
) -> dict[str, Any]:
    """Main handler — assumes HMAC has already been verified by the caller.

    Form-provided display_name + website_url override the values Stripe
    captured at checkout. The form is the user's final say on what
    appears on /supporters.
    """
    if not SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(400, "missing or malformed session_id")

    # Read file bytes once, with size cap.
    chunk_bytes = await logo.read(MAX_LOGO_BYTES + 1)
    if len(chunk_bytes) > MAX_LOGO_BYTES:
        raise HTTPException(413, "logo exceeds 5 MB limit")
    if not chunk_bytes:
        raise HTTPException(400, "empty logo file")

    sniffed_mime = _sniff_mime(chunk_bytes[:64])
    if sniffed_mime not in ALLOWED_MIME_EXT:
        raise HTTPException(
            415,
            "unrecognised image format — supply PNG, JPEG, SVG, or WebP",
        )
    ext = ALLOWED_MIME_EXT[sniffed_mime]

    session = _retrieve_checkout_session(session_id)
    info = _verify_sponsor_session(session)

    # Form values override Stripe-captured custom_fields when present.
    # The receiver still trusts the Stripe verification (paid + sponsor
    # tier + valid subscription), but the user's last word on the
    # public listing wins.
    display_name = (form_display_name or "").strip() or info["display_name"]
    if not (2 <= len(display_name) <= 80):
        raise HTTPException(400, "display_name must be 2–80 chars")

    website_url = (form_website_url or "").strip() or info["website_url"]
    if not re.match(r"^https?://", website_url, re.IGNORECASE):
        website_url = "https://" + website_url
    if not (10 <= len(website_url) <= 200):
        raise HTTPException(400, "website_url must be 10–200 chars after normalisation")

    # Filename: opaque 8-char session-id hash. Stable for re-uploads.
    session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:8]
    out_name = f"{session_hash}.{ext}"
    out_path = LOGO_DIR / out_name
    LOGO_DIR.mkdir(parents=True, exist_ok=True)

    # Write atomically — temp file + rename, so a partial write can't
    # leave a half-bytes logo on disk.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_bytes(chunk_bytes)
    tmp_path.replace(out_path)

    # If this session previously uploaded under a different extension
    # (e.g. PNG then later SVG), purge the stale variants so git diff
    # doesn't accumulate orphans.
    for other_ext in ALLOWED_MIME_EXT.values():
        if other_ext == ext:
            continue
        stale = LOGO_DIR / f"{session_hash}.{other_ext}"
        if stale.exists():
            stale.unlink()

    logo_path_rel = f"public/supporter-logos/{out_name}"

    supporters = _load_supporters()
    _upsert_sponsor(
        supporters,
        stripe_sub=info["stripe_sub"],
        display_name=display_name,
        website_url=website_url,
        logo_path="/" + logo_path_rel.removeprefix("public/"),
        valid_through=info["valid_through"],
    )
    _write_supporters(supporters)

    _git_commit_push(logo_path_rel, display_name)

    return {
        "ok": True,
        "display_name": display_name,
        "logo_path": "/" + logo_path_rel.removeprefix("public/"),
        "committed_at": int(time.time()),
    }
