#!/usr/bin/env python3
"""Workers AI and Mistral as panel providers, and the usage guard that keeps
Workers AI inside its free daily allocation and Mistral under its monthly cap. Offline: the HTTP call is stubbed, nothing is sent, and the
ledger lives in a temporary directory that is removed at the end, pass or fail.
Run from the repo root: .venv/bin/python intake/scripts/test_workers_ai.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


TMP = Path(tempfile.mkdtemp(prefix="icsac-wai-"))
try:
    os.environ["ICSAC_STATE_DIR"] = str(TMP)
    os.environ["TEST_WAI_KEY"] = "test-key"
    import config
    import review
    import redaction

    RATES = {"@cf/x/model-a": (31818, 68182)}
    SPEC = {"base_url": "https://example.invalid/ai/v1", "key_env": "TEST_WAI_KEY",
            "daily_neuron_cap": 9000, "neuron_rates": RATES}
    config.OAI_COMPAT_PROVIDERS["cftest"] = SPEC
    ledger_path = Path(review._usage_ledger_path("cftest"))

    sent = []
    reply = {"usage": {"prompt_tokens": 7000, "completion_tokens": 900},
             "choices": [{"message": {"content": "{}"}}]}
    mode = {"http": None}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent.append(req.full_url)
        if mode["http"]:
            raise urllib.error.HTTPError(req.full_url, mode["http"], "err", {}, io.BytesIO(b"{}"))
        return _Resp(json.dumps(reply).encode())
    urllib.request.urlopen = fake_urlopen

    def ledger_today():
        if not ledger_path.exists():
            return 0
        d = json.loads(ledger_path.read_text())
        return d.get(review.datetime.now(review.timezone.utc).strftime("%Y-%m-%d"), 0)

    prompt = "x" * 30000   # estimate: 30000/3 tokens in + 4000 out = ceil(318.18 + 272.73) = 591

    print("[1] a call within the allocation is sent and settled to the reported usage")
    r = review.run_oai_compat_review(prompt, "cftest", "@cf/x/model-a")
    check(len(sent) == 1 and sent[0].endswith("/chat/completions"), "one request sent")
    check(ledger_today() == 285, f"ledger = ceil(7000 in + 900 out at the model's rates) = 285 ({ledger_today()})")

    print("[2] a call that could cross the cap is refused before anything is sent")
    ledger_path.write_text(json.dumps({review.datetime.now(review.timezone.utc).strftime("%Y-%m-%d"): 8500}))
    sent.clear()
    r = review.run_oai_compat_review(prompt, "cftest", "@cf/x/model-a")
    check(not sent and "free-allocation guard" in r.get("error", "") and "8500 used + 591 estimated" in r["error"],
          f"refused, nothing sent ({r.get('error', '')[:90]})")
    check(ledger_today() == 8500, "a refusal reserves nothing")

    print("[3] a model with no rate on file is refused")
    ledger_path.unlink()
    r = review.run_oai_compat_review(prompt, "cftest", "@cf/x/unpriced")
    check(not sent and "rate on file" in r.get("error", ""), "refused, nothing sent")

    print("[4] an HTTP 4xx releases the reservation; a reply without usage keeps it")
    mode["http"] = 401
    r = review.run_oai_compat_review(prompt, "cftest", "@cf/x/model-a")
    check(len(sent) == 1 and "HTTP 401" in r.get("error", "") and ledger_today() == 0, f"401: ledger back to 0 ({ledger_today()})")
    mode["http"] = None
    reply.pop("usage")
    r = review.run_oai_compat_review(prompt, "cftest", "@cf/x/model-a")
    check(ledger_today() == 591, f"no usage figures: the 591 reservation stands ({ledger_today()})")

    print("[5] a provider without a cap is never metered; the ledger keeps 7 days")
    check(review._usage_reserve("x", {"base_url": "u"}, "m", prompt, 4000) == (None, 0, ""), "no cap, no reservation")
    old = {f"2026-09-{d:02d}": 1 for d in range(1, 21)}
    ledger_path.write_text(json.dumps(old))
    review._usage_ledger_update("cftest", lambda l: None, 7)
    check(len(json.loads(ledger_path.read_text())) == 7, "pruned to the last 7 days")

    print("[6] configuration")
    import importlib.util
    spec = importlib.util.spec_from_file_location("config_example", REPO / "config.example.py")
    ex = importlib.util.module_from_spec(spec); spec.loader.exec_module(ex)
    cf = ex.OAI_COMPAT_PROVIDERS.get("cloudflare") or {}
    check(cf.get("key_env") == "CLOUDFLARE_AI_TOKEN" and cf.get("daily_neuron_cap") == 9000
          and "/ai/v1" in cf.get("base_url", ""), "config.example: the cloudflare provider, cap 9000 by default")
    roster_cf = [e.split("|", 2)[2] for slot in ex.OPENROUTER_MODELS for e in slot if e.startswith("oai|cloudflare|")]
    check(all(m in cf.get("neuron_rates", {}) for m in roster_cf), f"every cloudflare roster entry has a rate ({roster_cf})")
    mi = ex.OAI_COMPAT_PROVIDERS.get("mistral") or {}
    check(mi.get("monthly_cap_usd") == 8.0 and mi.get("key_env") == "MISTRAL_API_KEY", "config.example: mistral capped at $8/month by default")
    roster_mi = [e.split("|", 2)[2] for slot in ex.OPENROUTER_MODELS for e in slot if e.startswith("oai|mistral|")]
    check(all(m in mi.get("usd_rates", {}) for m in roster_mi), f"every mistral roster entry has a USD rate ({roster_mi})")
    check("@cf/" in redaction.FORBIDDEN_VENDOR_TOKENS, "the route prefix is a forbidden self-identification token")

    print("[7] a monthly USD cap (Mistral): metered in its own ledger, refused before it can pass the cap")
    reply["usage"] = {"prompt_tokens": 7000, "completion_tokens": 900}
    config.OAI_COMPAT_PROVIDERS["mtest"] = {"base_url": "https://example.invalid/v1", "key_env": "TEST_WAI_KEY",
                                            "monthly_cap_usd": 8.0, "usd_rates": {"m-medium": (2.0, 10.0)}}
    month = review.datetime.now(review.timezone.utc).strftime("%Y-%m")
    mpath = Path(review._usage_ledger_path("mtest"))
    cf_before = ledger_path.read_text() if ledger_path.exists() else ""
    sent.clear()
    r = review.run_oai_compat_review(prompt, "mtest", "m-medium")
    m = json.loads(mpath.read_text())
    check(len(sent) == 1 and m == {month: 0.023}, f"settled to 7000 in + 900 out at $2/$10 per M = $0.023 ({m})")
    check((ledger_path.read_text() if ledger_path.exists() else "") == cf_before and mpath.name == "mtest_usage.json",
          "its own ledger file; the neuron ledger is untouched")
    mpath.write_text(json.dumps({month: 7.95}))
    sent.clear()
    r = review.run_oai_compat_review(prompt, "mtest", "m-medium")
    check(not sent and "monthly spending guard" in r.get("error", "") and "$7.9500 used + $0.0600 estimated > $8.00" in r["error"],
          f"refused before sending ({r.get('error', '')[:100]})")
    check(json.loads(mpath.read_text()) == {month: 7.95}, "a refusal reserves nothing")
    check(review._model_identity("cloudflare:@cf/openai/gpt-oss-120b") == review._model_identity("hf|openai/gpt-oss-120b:deepinfra"),
          "the same model served by two providers is one reviewer")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
