#!/usr/bin/env python3
"""Checks that a Stripe sponsor-logo upload is held for the curator (nothing
publishes without the curator's approval) and that --apply
publishes it only on the typed word "publish".
Offline: Stripe, Telegram and the curator ping are stubbed; the website repo
and its remote are temporary git repositories, removed at the end, pass or fail.
Run from the repo root:
    .venv/bin/python intake/scripts/test_sponsor_logo_hold.py
"""
from __future__ import annotations

import asyncio
import builtins
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          cwd=repo, capture_output=True, text=True, check=True).stdout


class FakeUpload:
    def __init__(self, data: bytes):
        self.data = data

    async def read(self, n: int = -1) -> bytes:
        return self.data[:n] if n and n > 0 else self.data


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1" fill="#000"/></svg>'
BAD_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
SESSION_ID = "cs_test_abc123"

tmp = Path(tempfile.mkdtemp(prefix="sponsor-logo-hold-"))
real_stdin, real_input = sys.stdin, builtins.input
try:
    from intake import sponsor_logo as sl
    import notify

    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    site = tmp / "site"
    subprocess.run(["git", "clone", "-q", str(remote), str(site)], check=True, capture_output=True)
    git(site, "checkout", "-q", "-b", "main")
    (site / "src" / "data").mkdir(parents=True)
    (site / "public" / "supporter-logos").mkdir(parents=True)
    (site / "public" / "supporter-logos" / ".keep").write_text("")
    yaml_path = site / "src" / "data" / "supporters.yaml"
    yaml_path.write_text(sl._render_supporters({"supporters": [
        {"display_name": "Example Sponsor Co", "tier": "sponsor", "joined": "2026-10-01", "status": "active",
         "stripe_sub": "sub_example", "website_url": "https://example.com", "logo_pending": True}]}))
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "seed")
    git(site, "push", "-q", "-u", "origin", "main")

    sl.WEBSITE_REPO = site
    sl.LOGO_DIR = site / "public" / "supporter-logos"
    sl.SUPPORTERS_YAML = yaml_path
    sl.READY_DIR = tmp / "ready"
    sl._retrieve_checkout_session = lambda sid: {
        "mode": "subscription", "payment_status": "paid",
        "subscription": {"id": "sub_example", "status": "active",
                         "items": {"data": [{"price": {"product": sl.SPONSOR_PRODUCT_ID}, "current_period_end": 4_102_444_800}]}},
        "custom_fields": [{"key": "display_name", "text": {"value": "Example Sponsor Co"}},
                          {"key": "website_url", "text": {"value": "example.com"}}]}
    pings: list[str] = []
    notify.send_to_curator = lambda message, **k: (pings.append(message), True)[1]
    images: list[tuple[str, str]] = []
    sl._send_logo_to_telegram = lambda image, caption: (images.append((image.name, caption)), True)[1]
    seed_head = git(site, "rev-parse", "HEAD").strip()
    seed_yaml = yaml_path.read_text()

    def upload(data: bytes) -> dict:
        return asyncio.run(sl.handle_sponsor_logo(None, SESSION_ID, FakeUpload(data), b""))

    print("1. an upload is held, not published")
    out = upload(PNG)
    held_json = sl.READY_DIR / f"{sl.STAGE_PREFIX}{__import__('hashlib').sha256(SESSION_ID.encode()).hexdigest()[:8]}.json"
    check(out.get("ok") is True and out.get("held") is True, "the sponsor gets a success answer marked held")
    check(git(site, "status", "--porcelain") == "", "the website repo is untouched")
    check(yaml_path.read_text() == seed_yaml, "supporters.yaml is unchanged")
    check(sorted(p.name for p in sl.LOGO_DIR.iterdir()) == [".keep"], "no logo file reaches the site")
    check(git(site, "rev-parse", "HEAD").strip() == seed_head, "no commit was made")
    check(held_json.exists() and held_json.with_suffix(".png").exists(), "the logo and its listing wait in ready-to-publish")
    rec = json.loads(held_json.read_text())
    check(rec["display_name"] == "Example Sponsor Co" and rec["website_url"] == "https://example.com"
          and rec["stripe_sub"] == "sub_example", "the held listing carries the Stripe-verified details")
    check(len(pings) == 1 and "Example Sponsor Co" not in pings[0] and "--apply" in pings[0],
          "one curator ping, with no sponsor name, naming the apply command")
    check(len(images) == 1 and images[0][0].endswith(".png") and "Example Sponsor Co" in images[0][1],
          "the image itself goes to the curator's Telegram")

    print("2. a re-upload replaces the held one")
    upload(SVG)
    check(held_json.with_suffix(".svg").exists() and not held_json.with_suffix(".png").exists(),
          "the SVG replaced the held PNG")
    check(json.loads(held_json.read_text())["logo_ext"] == "svg", "the held listing points at the new file")

    print("3. an active SVG is refused before anything is held")
    try:
        upload(BAD_SVG)
        check(False, "a scripted SVG is refused")
    except HTTPException as e:
        check(e.status_code == 415, "a scripted SVG is refused (415)")
    check(json.loads(held_json.read_text())["logo_ext"] == "svg", "the held upload is left as it was")

    print("4. apply refuses without a terminal or the typed word")
    sys.stdin = types.SimpleNamespace(isatty=lambda: False)
    check(sl.main(["--apply", str(held_json)]) == 1, "no terminal: refused")
    sys.stdin = types.SimpleNamespace(isatty=lambda: True)
    builtins.input = lambda prompt="": "yes"
    check(sl.main(["--apply", str(held_json)]) == 1, "a word other than publish: refused")
    check(git(site, "status", "--porcelain") == "" and yaml_path.read_text() == seed_yaml, "still nothing written")

    print("5. a held image that changed after the upload is refused")
    svg_path = held_json.with_suffix(".svg")
    svg_path.write_bytes(SVG.replace(b"#000", b"#fff"))
    builtins.input = lambda prompt="": "publish"
    check(sl.main(["--apply", str(held_json)]) == 1, "sha mismatch: refused")
    svg_path.write_bytes(SVG)

    print("6. publish writes the logo and the listing, commits as ICSAC, pushes")
    check(sl.main(["--apply", str(held_json)]) == 0, "apply returns 0")
    data = __import__("yaml").safe_load(yaml_path.read_text())
    entry = data["supporters"][0]
    logo_name = held_json.stem.removeprefix(sl.STAGE_PREFIX) + ".svg"
    check(entry.get("logo_path") == f"/supporter-logos/{logo_name}" and "logo_pending" not in entry,
          "the record now points at the logo; logo_pending cleared")
    check((sl.LOGO_DIR / logo_name).read_bytes() == SVG, "the logo file is on the site")
    check(git(site, "log", "-1", "--format=%an <%ae>").strip() == "ICSAC <info@icsacinstitute.org>", "committed as ICSAC")
    check(git(remote, "rev-parse", "main").strip() == git(site, "rev-parse", "HEAD").strip(), "pushed to the remote")
    check(not held_json.exists() and held_json.with_name(held_json.name + ".applied").exists()
          and svg_path.with_name(svg_path.name + ".applied").exists(), "the held files are marked applied")

    print("7. the upload path itself can never publish")
    src = inspect.getsource(sl.handle_sponsor_logo)
    check(not any(w in src for w in ("_git_output", "_write_supporters", "push")), "handle_sponsor_logo has no git or YAML write")
finally:
    sys.stdin, builtins.input = real_stdin, real_input
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(0 if not failures else 1)
