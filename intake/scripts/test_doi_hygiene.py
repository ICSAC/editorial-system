#!/usr/bin/env python3
"""Checks that the system never looks up one of its own DOIs before Crossref
has registered it. A doi.org lookup of a DOI that does not exist yet counts as
a failed resolution in Crossref's monthly report for the prefix, so:

  - curator pings go out with link previews off: Telegram would otherwise
    fetch every link in a message, a doi.org link included;
  - test fixtures never use the institute's live prefix: a fixture DOI under it
    names a paper that does not exist, or a real one once the sequence gets there.

Offline: the Telegram request is captured, never sent. Run from the repo root:
    .venv/bin/python intake/scripts/test_doi_hygiene.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


import config  # noqa: E402
import notify  # noqa: E402

# ── 1. Telegram link previews are off ────────────────────────────────────────
sent: list[dict] = []


class _Resp:
    status = 200

    def __init__(self, n: int):
        self._n = n

    def read(self) -> bytes:
        return json.dumps({"ok": True, "result": {"message_id": self._n}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(req, timeout=None):
    sent.append({"url": req.full_url, "body": json.loads(req.data.decode())})
    return _Resp(len(sent))


real_urlopen = notify.urllib.request.urlopen
saved = {k: getattr(config, k, None) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_THREAD_ID")}
notify.urllib.request.urlopen = fake_urlopen
try:
    config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.TELEGRAM_THREAD_ID = "t0k", "-100", "7"
    text = "DOI REGISTERED for ICSAC-SUB-TEST-1: https://doi.org/10.5555/icsac.2026.999"
    check(notify.send_telegram(text, parse_mode=None) == 1, "plain ping: sent, message id returned")
    check(notify.send_telegram(text) == 2, "Markdown ping: sent")
    check(notify.send_telegram(text, parse_mode=None, chat_override="-200", thread_override="9") == 3,
          "test-tier ping: sent to the override chat")
    check(notify.send_telegram(text, chat_override="__suppress__") is None and len(sent) == 3,
          "suppressed ping: nothing sent")
    check(len(sent) == 3 and all(s["url"].endswith("/sendMessage") for s in sent),
          "three requests, all sendMessage")
    check(all(s["body"].get("link_preview_options") == {"is_disabled": True} for s in sent),
          "every request turns link previews off")
    check(all(s["body"].get("text") == text for s in sent), "the text goes out unchanged")
    check(sent[1]["body"].get("parse_mode") == "Markdown" and "parse_mode" not in sent[0]["body"],
          "parse mode still applies only when asked")
    check(sent[0]["body"].get("message_thread_id") == 7
          and sent[2]["body"].get("chat_id") == "-200" and sent[2]["body"].get("message_thread_id") == 9,
          "the configured thread and the test-tier override still apply")
finally:
    notify.urllib.request.urlopen = real_urlopen
    for k, v in saved.items():
        setattr(config, k, v)

# ── 2. no test fixture uses the live prefix ──────────────────────────────────
prefix = str(getattr(config, "CROSSREF_PREFIX", "") or "")
check(bool(re.fullmatch(r"10\.\d{4,9}", prefix)), f"the live prefix is configured ({prefix or 'unset'})")
here = Path(__file__).resolve()
hits = []
for t in sorted(here.parent.glob("test_*.py")):
    if t == here:
        continue
    for n, line in enumerate(t.read_text(errors="replace").splitlines(), 1):
        if prefix and prefix + "/" in line:
            hits.append(f"{t.name}:{n}")
check(not hits, "no fixture DOI under the live prefix" + (f": {', '.join(hits[:8])}" if hits else ""))

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
