#!/usr/bin/env python3
"""A slot's raw capture is filed under the model that answered.

The capture file was named after the first entry of the slot's chain. When
that entry was skipped or failed and the next one answered, the file still
carried the first entry's name, so the raw record named a model that never
reviewed the paper. Now the capture is named after the answering model and
opens with a header naming it and the chain; a slot with no answer is filed
as no-answer. Offline: every provider call is stubbed.

    .venv/bin/python intake/scripts/test_raw_capture_model.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import review  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def oai(prompt, provider, model, capture_path=None):
    if provider == "headprov":                       # the chain head: refused before any output
        return {"error": "HTTP 401: no key", "model": f"{provider}:{model}"}
    if provider == "badprov":                        # answered, but not a review
        review._write_raw(capture_path, "not json at all", "")
        return {"error": "No JSON found", "model": f"{provider}:{model}"}
    review._write_raw(capture_path, '{"overall_recommendation": "RECOMMEND"}', "")
    return {"model": f"{provider}:{model}", "overall_recommendation": "RECOMMEND"}


def claude(prompt, capture_path=None):
    review._write_raw(capture_path, '{"overall_recommendation": "REVIEW_FURTHER"}', "")
    return {"model": "claude", "overall_recommendation": "REVIEW_FURTHER"}


review.run_oai_compat_review = oai
review.run_claude_review = claude
rid = "ICSAC-SUB-TEST-1790400001"

with tempfile.TemporaryDirectory(prefix="icsac-raw-") as td:
    config.REVIEWS_DIR = td
    raw = Path(td) / "raw" / rid

    print("[1] the chain head fails, the second entry answers")
    r = review._run_slot("p", 1, ["oai|headprov|model-a", "oai|nextprov|model-b"], record_id=rid, pass_idx=0)
    names = sorted(p.name for p in raw.iterdir()) if raw.exists() else []
    check("pass0_slot1_nextprov_model-b.txt" in names, f"filed under the answering model ({names})")
    check(not any("headprov" in n or "model-a" in n for n in names), "nothing filed under the chain head")
    body = (raw / "pass0_slot1_nextprov_model-b.txt").read_text() if "pass0_slot1_nextprov_model-b.txt" in names else ""
    check(body.startswith("answered_by: nextprov:model-b\n"), "the header names the answering model")
    check("chain: oai|headprov|model-a -> oai|nextprov|model-b" in body and "=== STDOUT ===" in body,
          "the header names the chain; the capture follows")
    check(r.get("model") == "nextprov:model-b" and "error" not in r, "the slot result is unchanged")

    print("[2] no entry answers")
    review._run_slot("p", 2, ["oai|headprov|model-a", "oai|badprov|model-c"], record_id=rid, pass_idx=0)
    names = sorted(p.name for p in raw.iterdir())
    check("pass0_slot2_no-answer.txt" in names, f"filed as no-answer ({names})")
    nb = (raw / "pass0_slot2_no-answer.txt").read_text() if "pass0_slot2_no-answer.txt" in names else ""
    check(nb.startswith("answered_by: none\n") and "error: No JSON found" in nb, "the header says nobody answered and why")

    print("[3] the Claude slot")
    review._run_slot("p", 0, None, record_id=rid, pass_idx=1)
    names = sorted(p.name for p in raw.iterdir())
    check("pass1_slot0_claude.txt" in names, f"claude capture ({names})")
    check(not any(n.endswith(".capture") for n in names), "no working file left behind")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
