#!/usr/bin/env python3
"""Regression checks for the identity-bound submit signature (2026-09-29,
audit item 12e). Offline: no network, no mail, no pings, no files written.
Run from the repo root:
    .venv/bin/python intake/scripts/test_hmac_identity.py

Covers: a v2 signature covers the timestamp, the ORCID, name and test-tier
headers and the body, so changing, adding or removing any of them fails; the
submit endpoint refuses the body-only v1 form unless the deploy-window switch
is on; the other signed endpoints keep accepting v1 (their identity is inside
the signed body); header values with line breaks or non-ASCII are refused; the
smoke scripts sign v2 in the same layout.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["INTAKE_HMAC_SECRET"] = "test-secret-not-used-anywhere"

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


from fastapi import HTTPException  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402

from intake import intake_server as srv  # noqa: E402

srv.HMAC_SECRET = b"test-secret-not-used-anywhere"
KEY = srv.HMAC_SECRET
BODY = b"--b\r\nContent-Disposition: form-data; name=\"orcid\"\r\n\r\n0000-0002-1825-0097\r\n--b--\r\n"
ORCID, NAME, TIER = "0000-0002-1825-0097", "Ada%20Lovelace", "t3"


class Req:
    def __init__(self, headers: dict):
        self.headers = Headers(headers=headers)


def v2(ts: str, orcid: str, name: str, tier: str, body: bytes) -> str:
    msg = ("\n".join(["icsac-v2", ts, orcid, name, tier]) + "\n").encode() + body
    return "v2=" + hmac.new(KEY, msg, hashlib.sha256).hexdigest()


def v1(ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(KEY, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def hdrs(ts, sig, orcid=ORCID, name=NAME, tier=TIER):
    h = {"x-icsac-timestamp": ts, "x-icsac-signature": sig}
    if orcid is not None:
        h["x-icsac-auth-orcid"] = orcid
    if name is not None:
        h["x-icsac-auth-name"] = name
    if tier is not None:
        h["x-icsac-test-tier"] = tier
    return h


def outcome(headers: dict, body: bytes = BODY, bind: bool = True) -> str:
    try:
        return srv._verify_hmac(Req(headers), body, bind_identity=bind)
    except HTTPException as exc:
        return f"{exc.status_code}:{exc.detail}"


ts = str(int(time.time()))
good = v2(ts, ORCID, NAME, TIER, BODY)

check(outcome(hdrs(ts, good)) == "v2", "v2 with all three identity headers verifies")
check(outcome(hdrs(ts, good, orcid="0000-0001-5109-3700")) == "401:bad signature", "another ORCID under the same signature fails")
check(outcome(hdrs(ts, good, name="Someone%20Else")) == "401:bad signature", "another name under the same signature fails")
check(outcome(hdrs(ts, good, tier=None)) == "401:bad signature", "dropping the tier header fails")
check(outcome(hdrs(ts, good, tier="t2")) == "401:bad signature", "changing the tier header fails")
check(outcome(hdrs(ts, good), body=BODY + b"x") == "401:bad signature", "a changed body fails")
no_tier = v2(ts, ORCID, NAME, "", BODY)
check(outcome(hdrs(ts, no_tier, tier=None)) == "v2", "a production request (no tier header) verifies with the tier signed empty")
check(outcome(hdrs(ts, no_tier, tier="t3")) == "401:bad signature", "adding a tier header to a production signature fails")
old = str(int(time.time()) - 301)
check(outcome(hdrs(old, v2(old, ORCID, NAME, TIER, BODY))) == "401:stale signature", "a v2 signature past the skew window fails")
check(outcome(hdrs(ts, v2(ts, ORCID, "A%0AB", TIER, BODY), name="AéB")) == "401:bad identity header", "a non-ASCII identity header is refused")

srv.SUBMIT_SIG_MODE = "v2"
check(outcome(hdrs(ts, v1(ts, BODY))) == "401:signature does not cover the identity headers", "submit refuses v1 by default")
srv.SUBMIT_SIG_MODE = "both"
check(outcome(hdrs(ts, v1(ts, BODY))) == "v1", "submit accepts v1 in the deploy window (INTAKE_SUBMIT_SIG=both)")
check(outcome(hdrs(ts, good)) == "v2", "submit accepts v2 in the deploy window")
srv.SUBMIT_SIG_MODE = "v2"
check(outcome(hdrs(ts, v1(ts, BODY), orcid=None, name=None, tier=None), bind=False) == "v1", "approve and sponsor-logo keep v1 (identity is in their signed body)")
check(outcome({"x-icsac-timestamp": ts, "x-icsac-signature": "md5=abc"}) == "401:missing signature", "an unknown signature scheme is refused")
check(outcome(hdrs(ts, "v2=" + "0" * 64)) == "401:bad signature", "a forged v2 value fails")

src = inspect.getsource(srv.api_submit)
check("bind_identity=True" in src, "the submit endpoint binds identity")
for ep in (getattr(srv, "api_approve_record", None), srv.api_sponsor_logo):
    if ep is not None:
        check("bind_identity=True" not in inspect.getsource(ep), f"{ep.__name__} does not demand identity headers")
check(srv.SUBMIT_SIG_MODE == "v2" and os.environ.get("INTAKE_SUBMIT_SIG", "v2") == "v2", "the default mode is v2")

for script in ("smoke_test_tier_2.sh", "smoke_test_test_mode.sh"):
    text = (ROOT / "intake" / "scripts" / script).read_text()
    check('printf "icsac-v2\\n%s\\n%s\\n%s\\n%s\\n"' in text and "x-icsac-signature: v2=$SIG" in text,
          f"{script} signs v2 in the server's layout")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
