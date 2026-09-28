#!/usr/bin/env python3
"""Offline tests for panel_retry.py (re-queue + the 24-hour scream) and for the
`oai|` backend dispatch in review._run_panel_chain. No network, no Telegram.
Run from the repo root: .venv/bin/python intake/scripts/test_panel_retry.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import panel_retry as pr   # noqa: E402

NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def mk(root, sub_id, state, test_mode=False):
    d = root / sub_id
    d.mkdir(parents=True)
    sub = {"sub_id": sub_id, "title": sub_id}
    if test_mode:
        sub["test_mode"] = True; sub["tier"] = 3
    (d / "submission.json").write_text(json.dumps(sub))
    (d / "state.json").write_text(json.dumps(state))
    return d


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pr-test-"))
    prod = tmp / "subs"; test = prod / "test"
    (prod / "queue").mkdir(parents=True); (test / "queue").mkdir(parents=True)
    pr.SUBMISSIONS_ROOT, pr.QUEUE_DIR = prod, prod / "queue"
    pr.TEST_SUBMISSIONS_ROOT, pr.TEST_QUEUE_DIR = test, test / "queue"
    pr.AUDIT_LOG, pr.TEST_AUDIT_LOG = tmp / "audit.jsonl", tmp / "audit-test.jsonl"
    tg, pain = [], []
    pr._telegram = lambda m: tg.append(m)
    pr._pain = lambda t, m: pain.append((t, m))
    h = lambda n: iso(NOW - dt.timedelta(hours=n))  # noqa: E731

    P1 = mk(prod, "ICSAC-SUB-00201", {"state": "paused_panel_failure", "panel_paused_at": h(2), "review_started_at": h(2.3), "received_at": h(40)})
    P2 = mk(prod, "ICSAC-SUB-00202", {"state": "paused_panel_failure", "panel_paused_at": h(1), "panel_wait_started_at": h(30), "panel_retry_count": 2})
    P3 = mk(prod, "ICSAC-SUB-00203", {"state": "paused_panel_failure", "panel_paused_at": h(5)})
    (prod / "queue" / P3.name).write_text("x")           # already queued
    S1 = mk(prod, "ICSAC-SUB-00204", {"state": "in_review", "review_started_at": h(5)})
    R1 = mk(prod, "ICSAC-SUB-00205", {"state": "in_review", "review_started_at": iso(NOW - dt.timedelta(minutes=10))})
    D1 = mk(prod, "ICSAC-SUB-00206", {"state": "awaiting_decision", "panel_completed_at": h(1)})
    D2 = mk(prod, "ICSAC-SUB-00207", {"state": "paused_panel_failure", "panel_paused_at": h(50), "decision": "accept"})
    T1 = mk(test, "ICSAC-SUB-TEST-1700000001", {"state": "paused_panel_failure", "panel_paused_at": h(30), "test_mode": True}, test_mode=True)

    failures = []
    def check(c, m):
        (failures if not c else []).append(m); print(("  ok   " if c else "  FAIL ") + m)
    st = lambda d: json.loads((d / "state.json").read_text())  # noqa: E731

    check(pr.classify(st(P1), NOW)[0] == "paused", "paused paper classified")
    check(pr.classify(st(S1), NOW)[0] == "stuck", "in_review for 5 h is stuck")
    check(pr.classify(st(R1), NOW)[0] == "running", "in_review for 10 min is running")
    check(pr.classify(st(D1), NOW)[0] == "done" and pr.classify(st(D2), NOW)[0] == "done", "decided / awaiting are done")
    check(pr.wait_started(st(P1), "paused") == pr._parse(h(2)), "wait clock starts at the first pause, not receipt")

    s = pr.tick(NOW, log=lambda m: None)
    check(s["checked"] == 4, f"P1 P2 P3 S1 checked ({s['checked']})")
    check(s["requeued"] == 3, f"P1 P2 S1 re-queued, P3 left alone ({s['requeued']})")
    check((prod / "queue" / P1.name).exists() and (prod / "queue" / S1.name).exists(), "markers written for P1 and S1")
    check((prod / "queue" / P3.name).read_text() == "x", "existing marker untouched")
    check(st(P1)["panel_retry_count"] == 1 and st(P1)["panel_wait_started_at"] == h(2), "P1 retry counted, clock = first pause")
    check(st(P2)["panel_retry_count"] == 3 and st(P2)["panel_wait_started_at"] == h(30), "P2 keeps its original clock")
    check(st(S1)["panel_wait_started_at"] == h(5), "stuck paper's clock = review start")
    check(s["screamed"] == 2, f"P2 (30 h) and S1 (stuck) screamed; P1 (2 h) did not ({s['screamed']})")
    check(len(tg) == 2 and len(pain) == 2 and all("PANEL SCREAM" in m for m in tg), "scream = Telegram + pain, once each")
    check(any("30 h" in m and "Re-queued 3" in m for m in tg), "scream names the hours and the retries")
    check(st(P2)["panel_scream_count"] == 1 and st(P1).get("panel_scream_count") is None, "scream counted on P2 only")
    check(not (test / "queue" / T1.name).exists(), "test papers untouched without include_test")
    audit = [json.loads(l) for l in (tmp / "audit.jsonl").read_text().splitlines()]
    check(sum(a["event"] == "panel_requeued" for a in audit) == 3 and sum(a["event"] == "panel_scream" for a in audit) == 2, "audit has 3 requeues + 2 screams")

    # second tick, same clock: markers exist -> no double queueing; P2 screams again (every tick while waiting)
    n = len(tg)
    s2 = pr.tick(NOW, log=lambda m: None)
    check(s2["requeued"] == 0, "nothing re-queued while markers are still in the queue")
    check(len(tg) == n + 2 and st(P2)["panel_scream_count"] == 2, "screams repeat every tick while the paper waits")

    # the worker took P1 and paused it again 20 h later: still counted from the FIRST pause
    (prod / "queue" / P1.name).unlink()
    pr._update_state(P1, state="paused_panel_failure", panel_paused_at=iso(NOW + dt.timedelta(hours=20)))
    later = NOW + dt.timedelta(hours=23)
    n = len(tg)
    s3 = pr.tick(later, log=lambda m: None)
    check(st(P1)["panel_retry_count"] == 2 and st(P1)["panel_wait_started_at"] == h(2), "P1 re-queued again; clock unchanged")
    check(any(P1.name in m and "25 h" in m for m in tg[n:]), "P1 screams at 25 h of waiting (2 h + 23 h), not from the latest pause")

    # test tier with include_test: re-queued into the test queue, no pings
    n = len(tg); p = len(pain)
    s4 = pr.tick(NOW, include_test=True, log=lambda m: None)
    check((test / "queue" / T1.name).exists() and not any(T1.name in m for m in tg[n:]) and not any(T1.name in m for _t, m in pain[p:]), "test paper re-queued into test/queue with no Telegram or pain (prod papers still scream)")
    check(all(json.loads(l).get("test") for l in (tmp / "audit-test.jsonl").read_text().splitlines()), "test audit lines flagged test")

    # legacy: a paper paused before the feature existed is announced once and left parked
    L1 = mk(prod, "ICSAC-SUB-00210", {"state": "paused_panel_failure", "panel_paused_at": "2026-04-30T01:30:23Z", "review_started_at": "2026-04-30T01:25:00Z"})
    n = len(tg)
    s7 = pr.tick(NOW, log=lambda m: None)
    check(not (prod / "queue" / L1.name).exists() and st(L1).get("panel_retry_count") is None, "legacy paper is not re-queued")
    check(st(L1).get("panel_legacy_notified_at") and sum(L1.name in m and "before automatic retries" in m for m in tg[n:]) == 1, "legacy paper announced once")
    n = len(tg)
    pr.tick(NOW + dt.timedelta(hours=12), log=lambda m: None)
    check(not any(L1.name in m for m in tg[n:]), "legacy paper is silent on the next tick")

    # the retry cap: a paper re-queued MAX_RETRIES times keeps screaming but is not re-run
    C1 = mk(prod, "ICSAC-SUB-00209", {"state": "paused_panel_failure", "panel_paused_at": h(1), "panel_wait_started_at": h(30), "panel_retry_count": pr.MAX_RETRIES})   # after RETRY_SINCE, so not legacy
    n = len(tg)
    s6 = pr.tick(NOW, log=lambda m: None)
    check(not (prod / "queue" / C1.name).exists() and st(C1)["panel_retry_count"] == pr.MAX_RETRIES, "capped paper is not re-queued")
    check(any(C1.name in m and "the cap" in m for m in tg[n:]), "capped paper still screams and says so")

    # dry run writes nothing
    P4 = mk(prod, "ICSAC-SUB-00208", {"state": "paused_panel_failure", "panel_paused_at": h(40)})
    n = len(tg)
    s5 = pr.tick(NOW, dry_run=True, log=lambda m: None)
    check(not (prod / "queue" / P4.name).exists() and st(P4).get("panel_retry_count") is None and len(tg) == n, "dry run: no marker, no state, no ping")

    # ── review.py: the oai| backend is dispatched and short-circuits the chain ──
    import review
    calls = []
    review.run_oai_compat_review = lambda prompt, provider, model, capture_path=None: (calls.append(("oai", provider, model)) or {"model": f"{provider}:{model}", "scores": {}})
    review.run_hf_router_review = lambda prompt, model, capture_path=None: (calls.append(("hf", model)) or {"error": "HTTP 402", "model": model})
    review.run_openrouter_review = lambda prompt, models, capture_path=None: (calls.append(("or", tuple(models))) or {"error": "429", "model": "or"})
    r = review._run_panel_chain("p", ["hf|a/b:deepinfra", "oai|sambanova|DeepSeek-V3.1", "or|x:free"])
    check(r.get("model") == "sambanova:DeepSeek-V3.1" and calls == [("hf", "a/b:deepinfra"), ("oai", "sambanova", "DeepSeek-V3.1")], f"chain: hf fails, oai answers, OR never called ({calls})")
    calls.clear()
    review.run_oai_compat_review = lambda prompt, provider, model, capture_path=None: (calls.append(("oai", provider, model)) or {"error": "SAMBANOVA_API_KEY not set", "model": f"{provider}:{model}"})
    r = review._run_panel_chain("p", ["oai|sambanova|DeepSeek-V3.1", "or|x:free", "or|y:free"])
    check("error" in r and calls == [("oai", "sambanova", "DeepSeek-V3.1"), ("or", ("x:free", "y:free"))], f"chain: unconfigured oai falls through to a batched OR call ({calls})")

    print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s) in {tmp}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
