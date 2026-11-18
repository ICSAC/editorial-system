#!/usr/bin/env python3
"""No majority = REVIEW_FURTHER.

When neither the RECOMMEND votes nor the decline votes (REVISE_AND_RESUBMIT +
REJECT) are a strict majority of the valid votes, the headline is
REVIEW_FURTHER. It is checked after the REJECT and R&R majority rules and
before the RECOMMEND score test, so strong scores no longer break a split
panel toward RECOMMEND. Per-pass and multipass aggregates both go through it.
Offline.

    .venv/bin/python intake/scripts/test_panel_tie_rule.py
"""
from __future__ import annotations

import sys
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


# Scores that pass the RECOMMEND score test on their own (mean 4.0, min 3.0, domain fit 4.5).
GOOD = {d: {"mean": 4.0, "mean_raw": 4.0} for d in config.RUBRIC_DIMENSIONS}
GOOD["domain_fit"] = {"mean": 4.5, "mean_raw": 4.5}
GOOD["novelty_signal"] = {"mean": 3.0, "mean_raw": 3.0}


def votes(rec=0, rr=0, rf=0, rej=0) -> list[str]:
    return ["RECOMMEND"] * rec + ["REVISE_AND_RESUBMIT"] * rr + ["REVIEW_FURTHER"] * rf + ["REJECT"] * rej


print("[1] the table")
TABLE = [
    (votes(rr=5, rec=4, rf=1), "REVIEW_FURTHER", "5 R&R / 4 REC / 1 RF"),
    (votes(rec=6, rr=4), "RECOMMEND", "6 REC / 4 R&R -> the score test"),
    (votes(rr=6, rec=4), "REVISE_AND_RESUBMIT", "6 R&R / 4 REC -> R&R majority"),
    (votes(rec=5, rr=5), "REVIEW_FURTHER", "5 REC / 5 R&R"),
    (votes(rec=10), "RECOMMEND", "10 REC -> the score test"),
    (votes(rec=4, rr=3, rf=3), "REVIEW_FURTHER", "4 REC / 3 R&R / 3 RF"),
    (votes(rec=3, rr=2), "RECOMMEND", "3 REC / 2 R&R (one pass of five)"),
    (votes(rec=2, rr=2, rf=1), "REVIEW_FURTHER", "2 REC / 2 R&R / 1 RF (one pass of five)"),
    (votes(rej=7, rec=3), "REJECT", "7 REJECT -> the majority-reject override still comes first"),
]
for recs, want, name in TABLE:
    got = review._apply_thresholds(GOOD, recs)
    check(got == want, f"{name}: {got} (want {want})")

print("[2] scores still matter once there is a majority")
weak = dict(GOOD)
weak["domain_fit"] = {"mean": 3.0, "mean_raw": 3.0}
check(review._apply_thresholds(weak, votes(rec=10)) == "REVIEW_FURTHER", "10 REC with domain fit 3.0 -> REVIEW_FURTHER")

print("[3] multipass and per-pass aggregates both use it")


def slot(rec: str) -> dict:
    r = {d: {"score": 4, "justification": "x"} for d in config.RUBRIC_DIMENSIONS}
    r["domain_fit"]["score"] = 5
    r["overall_recommendation"] = rec
    r["model"] = "m"
    return r


p1 = [slot(r) for r in votes(rr=3, rec=2)]
p2 = [slot(r) for r in votes(rr=2, rec=2, rf=1)]
agg = review.compute_aggregate_multipass([p1, p2])
check(agg["recommendation"] == "REVIEW_FURTHER", f"two passes, 5 R&R / 4 REC / 1 RF overall: {agg['recommendation']}")
check(agg["pass_aggregates"][0]["recommendation"] == "REVISE_AND_RESUBMIT"
      and agg["pass_aggregates"][1]["recommendation"] == "REVIEW_FURTHER",
      f"per pass: {[pa['recommendation'] for pa in agg['pass_aggregates']]}")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
