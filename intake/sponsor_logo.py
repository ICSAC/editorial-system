"""ICSAC sponsor-logo intake — handles the after-checkout logo upload.

Lives next to intake_server.py and gets wired in via a single import +
route registration there. Kept in its own module so the submission
intake stays focused; this module owns the website-repo side effects
(file write, supporters.yaml mutation, git commit + push), which run only
when the curator publishes a held upload.

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

An upload publishes nothing: nothing reaches the site without the curator's
approval. Side effects per successful upload:
  1. The logo and the proposed listing are held in the ready-to-publish
     directory as sponsor-logo-<8hex>.<ext> + sponsor-logo-<8hex>.json; a
     re-upload for the same session replaces the held one.
  2. The curator gets one ping (no sponsor name, since it also goes to the
     ntfy backup) and the image itself on Telegram, so it can be seen first.

Publishing, from the repo root:
    .venv/bin/python -m intake.sponsor_logo --apply <held .json>
shows the supporters.yaml diff, asks for the typed word "publish", then
writes public/supporter-logos/<8hex>.<ext> and the record (keyed by
stripe_sub; logo_pending cleared, logo_path, display_name and website_url
set), commits and pushes from the website repo.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
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

# Website repo checkout on the intake host. Its SSH remote uses a host
# alias that already carries a dedicated push key.
WEBSITE_REPO = Path.home() / "Desktop" / "icsac" / "icsacinstitute.org"
LOGO_DIR = WEBSITE_REPO / "public" / "supporter-logos"
SUPPORTERS_YAML = WEBSITE_REPO / "src" / "data" / "supporters.yaml"
# Held uploads wait for the curator here (the directory publications.py holds entries in).
READY_DIR = Path.home() / "icsac-submissions" / "ready-to-publish"
STAGE_PREFIX = "sponsor-logo-"

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

# A sponsor SVG is served from the site's own origin. Anything that can run or
# fetch is refused at upload: scripts, event handlers, foreign objects, entity
# declarations, external references (audit 2026-09-28 pass B). The site also
# serves /supporter-logos/* under a script-free Content-Security-Policy.
_SVG_ACTIVE_RE = re.compile(
    rb"(<\s*(script|foreignObject|iframe|embed|object)\b"
    rb"|\son[a-z]+\s*="
    rb"|javascript\s*:"
    rb"|<!ENTITY"
    rb"|(xlink:)?href\s*=\s*[\"']\s*(https?:|//)"
    rb"|@import"
    rb"|url\s*\(\s*[\"']?\s*(https?:|//))",
    re.IGNORECASE,
)


def _svg_is_inert(data: bytes) -> bool:
    return _SVG_ACTIVE_RE.search(data) is None

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
    # A paid checkout session outlives the subscription it started; the listing
    # may change only while that subscription is live (audit 2026-09-29).
    sub_status = subscription.get("status")
    if sub_status not in (None, "active", "trialing"):
        raise HTTPException(403, f"subscription is {sub_status}; the sponsor listing can be "
                                 "changed only while the subscription is active.")
    _cpe = subscription.get("current_period_end")
    if isinstance(_cpe, int) and _cpe < time.time() - 86400:
        raise HTTPException(403, "the subscription period has ended; the sponsor listing can be "
                                 "changed only while the subscription is active.")

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


def _render_supporters(data: dict[str, Any]) -> str:
    header = (
        "# ICSAC Supporters Registry\n"
        "# Written by sponsor-logo intake + Stripe poll job.\n"
        "# Schema: see commit history of this file.\n\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _write_supporters(data: dict[str, Any]) -> None:
    """Atomic write — temp file + rename so a crash mid-write can't corrupt."""
    SUPPORTERS_YAML.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUPPORTERS_YAML.with_suffix(SUPPORTERS_YAML.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(_render_supporters(data))
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


def _git_output(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=WEBSITE_REPO, capture_output=True,
                          text=True, check=False, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def _stage_upload(session_hash: str, ext: str, data: bytes, listing: dict[str, str]) -> tuple[Path, Path]:
    """Hold the logo and its proposed listing for the curator. Writes nothing to the site.
    A re-upload for the same session replaces the held one, whatever its extension."""
    READY_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{STAGE_PREFIX}{session_hash}"
    image = READY_DIR / f"{base}.{ext}"
    for other_ext in ALLOWED_MIME_EXT.values():
        stale = READY_DIR / f"{base}.{other_ext}"
        if other_ext != ext and stale.exists():
            stale.unlink()
    tmp = image.with_name(image.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(image)
    record = {
        "held_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": "Sponsor logo upload (Stripe checkout)",
        "session_hash": session_hash,
        "logo_file": image.name,
        "logo_ext": ext,
        "logo_sha256": hashlib.sha256(data).hexdigest(),
        **listing,
    }
    stage = READY_DIR / f"{base}.json"
    tmp = stage.with_name(stage.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(stage)
    return stage, image


def _send_logo_to_telegram(image: Path, caption: str) -> bool:
    """Show the curator the uploaded image itself. Telegram only: the ntfy backup carries no names."""
    import config  # noqa: PLC0415 — editorial root module, on sys.path in the server and with -m

    token = getattr(config, "TELEGRAM_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    data = {"chat_id": str(chat_id), "caption": caption[:1000]}
    thread_id = str(getattr(config, "TELEGRAM_THREAD_ID", "") or "").strip()
    if thread_id:
        data["message_thread_id"] = thread_id
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(f"https://api.telegram.org/bot{token}/sendDocument", data=data,
                               files={"document": (image.name, image.read_bytes())})
        return resp.status_code == 200 and bool(resp.json().get("ok"))
    except (httpx.HTTPError, ValueError) as e:
        # Only the type: the request URL carries the bot token.
        print(f"  sponsor-logo: Telegram image send failed ({type(e).__name__})")
        return False


def _ping_curator(stage: Path, image: Path, display_name: str) -> None:
    import notify  # noqa: PLC0415

    message = ("READY TO PUBLISH (held for your approval): a Sponsor logo upload from /sponsor-onboarding. "
               "Review and publish from the editorial-system directory: "
               f".venv/bin/python -m intake.sponsor_logo --apply {stage}")
    try:
        pinged = bool(notify.send_to_curator(message, parse_mode=None))
    except Exception as e:  # noqa: BLE001 — the upload is held either way
        print(f"  sponsor-logo: curator ping error ({type(e).__name__})")
        pinged = False
    if not pinged:
        print(f"  sponsor-logo: curator ping failed; the upload is still held at {stage}")
    if not _send_logo_to_telegram(image, f"Sponsor logo held for your approval: {display_name}"):
        print(f"  sponsor-logo: image not sent to Telegram; it is held at {image}")


def _apply_staged(stage: Path, push: bool = True) -> int:
    """Review a held upload and, on the typed word "publish", put it on the site."""
    held = json.loads(stage.read_text(encoding="utf-8"))
    session_hash = str(held.get("session_hash", ""))
    ext = str(held.get("logo_ext", ""))
    if not re.fullmatch(r"[0-9a-f]{8}", session_hash) or ext not in ALLOWED_MIME_EXT.values():
        print("sponsor-logo: the held file is malformed; nothing written", file=sys.stderr)
        return 1
    image = stage.with_name(f"{STAGE_PREFIX}{session_hash}.{ext}")
    data = image.read_bytes()
    if hashlib.sha256(data).hexdigest() != held.get("logo_sha256"):
        print("sponsor-logo: the held image changed since the upload; nothing written", file=sys.stderr)
        return 1
    mime = _sniff_mime(data[:64])
    if ALLOWED_MIME_EXT.get(mime or "") != ext or (mime == "image/svg+xml" and not _svg_is_inert(data)):
        print("sponsor-logo: the held image failed the format check; nothing written", file=sys.stderr)
        return 1
    if _git_output("status", "--porcelain").strip():
        print("sponsor-logo: the website repo has uncommitted changes; refusing to apply over them", file=sys.stderr)
        return 1
    if push:
        _git_output("pull", "--rebase", "origin", "main")

    before = SUPPORTERS_YAML.read_text(encoding="utf-8") if SUPPORTERS_YAML.exists() else ""
    supporters = _load_supporters()
    out_name = f"{session_hash}.{ext}"
    display_name = str(held.get("display_name", ""))
    _upsert_sponsor(
        supporters,
        stripe_sub=str(held.get("stripe_sub", "")),
        display_name=display_name,
        website_url=str(held.get("website_url", "")),
        logo_path=f"/supporter-logos/{out_name}",
        valid_through=str(held.get("valid_through", "")),
    )
    after = _render_supporters(supporters)
    logo_file = LOGO_DIR / out_name
    if after == before and logo_file.exists() and logo_file.read_bytes() == data:
        print(f"sponsor-logo: the site already shows {stage.name}; nothing to publish")
        _mark_applied(stage, image)
        return 0
    sys.stdout.writelines(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               "supporters.yaml (live)", "supporters.yaml (after)"))
    print(f"\nLogo: public/supporter-logos/{out_name} ({max(1, len(data) // 1024)} KB {ext}). "
          f"The image went to Telegram with the ping; held copy: {image}")
    if not sys.stdin.isatty():
        print("sponsor-logo: --apply needs a terminal to confirm; nothing written", file=sys.stderr)
        return 1
    word = input('Type "publish" to write the logo and supporters.yaml and '
                 + ("commit and push: " if push else "commit (no push): "))
    if word.strip() != "publish":
        print("sponsor-logo: not confirmed; nothing written")
        return 1

    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = logo_file.with_name(logo_file.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(logo_file)
    # An earlier upload under another extension would be left orphaned.
    for other_ext in ALLOWED_MIME_EXT.values():
        stale = LOGO_DIR / f"{session_hash}.{other_ext}"
        if other_ext != ext and stale.exists():
            stale.unlink()
    _write_supporters(supporters)
    _git_output("add", "-A", "--", "src/data/supporters.yaml", "public/supporter-logos")
    _git_output("-c", "user.email=info@icsacinstitute.org", "-c", "user.name=ICSAC",
                "commit", "-m", f"sponsors: add {display_name} logo")
    if push:
        _git_output("push", "origin", "main")
    _mark_applied(stage, image)
    print("sponsor-logo: published" if push else "sponsor-logo: committed (not pushed)")
    return 0


def _mark_applied(stage: Path, image: Path) -> None:
    for p in (stage, image):
        if p.exists():
            p.rename(p.with_name(p.name + ".applied"))


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
    if sniffed_mime == "image/svg+xml" and not _svg_is_inert(chunk_bytes):
        raise HTTPException(
            415,
            "SVG logos must not contain scripts, event handlers, foreign objects, "
            "entity declarations or external references",
        )

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

    # Held for the curator; nothing reaches the site until the curator publishes it.
    stage, image = _stage_upload(session_hash, ext, chunk_bytes, {
        "stripe_sub": info["stripe_sub"],
        "display_name": display_name,
        "website_url": website_url,
        "valid_through": info["valid_through"],
    })
    _ping_curator(stage, image, display_name)

    return {
        "ok": True,
        "held": True,
        "display_name": display_name,
        "received_at": int(time.time()),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Review and publish a held sponsor-logo upload.")
    ap.add_argument("--apply", type=Path, required=True, metavar="FILE", help="the held sponsor-logo-<id>.json")
    ap.add_argument("--no-push", action="store_true", help="commit but do not pull or push")
    args = ap.parse_args(argv)
    try:
        return _apply_staged(args.apply, push=not args.no_push)
    except Exception as e:  # noqa: BLE001
        print(f"sponsor-logo: apply FAILED: {getattr(e, 'detail', None) or e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
