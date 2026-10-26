#!/usr/bin/env python3
"""Author-facing letters carry no em dash (U+2014): every active template under
intake/templates/ (backups excluded) and the attachment list a decision letter
prints. Offline, no files written. Run from the repo root with the venv:
    .venv/bin/python intake/scripts/test_no_em_dash.py [templates-dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from intake import notify_author  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


templates = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "intake" / "templates"
active = sorted(p for p in templates.glob("*.md") if ".bak" not in p.name)
check(len(active) >= 10, f"{len(active)} active templates found")
for p in active:
    lines = [i for i, line in enumerate(p.read_text().splitlines(), 1) if "—" in line]
    check(not lines, f"{p.name}: no em dash" + (f" (lines {lines})" if lines else ""))

block = notify_author._attachments_block("ICSAC-SUB-00000", "# panel", "# rqc")
check("—" not in block, "attachment list: no em dash")

sys.exit(1 if failures else 0)
