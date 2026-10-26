#!/usr/bin/env python3
"""The misattribution check runs on the panel's own providers and never reads
an empty result as clean.

  - the chain tries the oai| providers (Workers AI, Mistral) before HF and OR,
    an oai| answer becomes verdicts, an answer covering only some pairs moves
    the chain on, and the fullest answer is kept when none is complete;
  - a direct provider with no key returns nothing and makes no request;
  - eligible references with no verdicts (no selection, or no model answered,
    or an exception) put a "could not run" section in the evidence, log an
    error in the audit event and ping the curator once; a test id stays quiet;
  - no identity-confirmed reference at all is a clean skip.
Offline: every provider call is replaced; audit lines go to a temporary dir.
    .venv/bin/python intake/scripts/test_misattribution_not_checked.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import notify  # noqa: E402
import citation_misattribution as cm  # noqa: E402
import review  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


CIT = {"raw": "Doe, J. (2020). A title. Journal.", "title": "A title", "authors": ["Doe"],
       "year": 2020, "claim_context": "the claim", "abstract": "an abstract",
       "resolved_id": "10.0000/example", "verified": True, "confidence": "exact-id"}
UNCONFIRMED = dict(CIT, verified=False, confidence="unverifiable")

calls: list[str] = []
cm._call_hf_router = lambda prompt, model: (calls.append(f"hf|{model}") or "")
cm._call_openrouter = lambda prompt, models: (calls.append("or|" + ",".join(models)) or "")
cm._call_oai_compat = lambda prompt, provider, model: (calls.append(f"oai|{provider}|{model}") or "")

# 1. chain order: the panel's providers first
out = cm.check_misattribution_batch([CIT], "text")
check(out == [], "no answer anywhere: no verdicts")
check(bool(calls) and calls[0] == "oai|cloudflare|@cf/zai-org/glm-4.7-flash", f"first call is Workers AI GLM (got {calls[:1]})")
check([c.split("|")[1] for c in calls[:3]] == ["cloudflare", "cloudflare", "mistral"], "then gpt-oss on Workers AI, then Mistral")
check(any(c.startswith("hf|") for c in calls) and calls[-1].startswith("or|"), "HF and OR stay as the tail")

# 2. an oai| answer becomes verdicts
cm._call_oai_compat = lambda prompt, provider, model: '[{"citation_id": 0, "supports": "no", "reason": "different claim"}]'
out = cm.check_misattribution_batch([CIT], "text")
check(len(out) == 1 and out[0]["supports"] == "no", "an oai| answer is parsed into a verdict")

# 2b. a partial answer moves the chain on; a complete one wins
CIT2 = dict(CIT, raw="Roe, R. (2021). Another. Journal.", title="Another", authors=["Roe"], year=2021)
one = '{"citation_id": 0, "supports": "yes", "reason": "r"}'
both = '{"verdicts": [{"citation_id": 0, "supports": "yes", "reason": "r"}, {"citation_id": 1, "supports": "unsure", "reason": "s"}]}'
tried: list[str] = []
cm._call_oai_compat = lambda prompt, provider, model: (tried.append(model) or (one if "glm" in model else both))
out = cm.check_misattribution_batch([CIT, CIT2], "text")
check(len(out) == 2 and tried[:2] == ["@cf/zai-org/glm-4.7-flash", "@cf/openai/gpt-oss-120b"],
      "a one-verdict answer for two pairs moves on; the verdicts object from the next model wins")
check(len(cm._parse_verdicts(one, [CIT, CIT2])) == 1, "a lone verdict object parses as one verdict")
cm._call_oai_compat = lambda prompt, provider, model: one
out = cm.check_misattribution_batch([CIT, CIT2], "text")
check(len(out) == 1, "no complete answer anywhere: the fullest partial answer is kept")

# 3. a direct provider without a key: nothing sent
saved = getattr(config, "OAI_COMPAT_PROVIDERS", {})
config.OAI_COMPAT_PROVIDERS = {"keyless": {"base_url": "http://127.0.0.1:9", "key_env": "ICSAC_TEST_UNSET_KEY"}}
import importlib  # noqa: E402
real = importlib.reload(cm)._call_oai_compat
check(real("p", "keyless", "m") == "", "no key: empty, no request")
check(real("p", "nosuch", "m") == "", "unknown provider: empty")
config.OAI_COMPAT_PROVIDERS = saved

# 4-7. the caller: could-not-check is loud, a clean skip stays quiet
pings: list[str] = []
notify.send_to_curator = lambda msg, *a, **k: pings.append(msg)
with tempfile.TemporaryDirectory() as tmp:
    config.REVIEWS_DIR = tmp

    def events(name="audit-log.jsonl"):
        p = Path(tmp) / name
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    cm.select_load_bearing = lambda citations, full_text, *a, **k: [CIT]
    cm.check_misattribution_batch = lambda lb, ft: []
    rep = review._run_citation_misattribution("ICSAC-SUB-99999", [CIT], "text", "REPORT")
    ev = events()[-1]
    check("could not run" in rep, "no verdicts: the evidence says the check could not run")
    check(bool(ev.get("error")) and "MisattributionNotChecked" in ev["error"], "no verdicts: the audit event carries an error")
    check(len(pings) == 1 and "ICSAC-SUB-99999" in pings[0], "no verdicts: one curator ping")

    cm.select_load_bearing = lambda citations, full_text, *a, **k: []
    rep = review._run_citation_misattribution("ICSAC-SUB-99998", [CIT], "text", "REPORT")
    check("could not run" in rep and "MisattributionNotChecked" in (events()[-1].get("error") or ""),
          "eligible references, no selection: could not check")

    def boom(*a, **k):
        raise ValueError("bad response")
    cm.select_load_bearing = boom
    rep = review._run_citation_misattribution("ICSAC-SUB-99997", [CIT], "text", "REPORT")
    check("could not run" in rep and "ValueError" in (events()[-1].get("error") or ""), "an exception: could not check")

    cm.select_load_bearing = lambda citations, full_text, *a, **k: [CIT, CIT2]
    cm.check_misattribution_batch = lambda lb, ft: [{"citation_id": 0, "label": "Doe 2020", "claim_context": "",
                                                     "supports": "yes", "reason": "r", "resolved_id": None}]
    rep = review._run_citation_misattribution("ICSAC-SUB-99995", [CIT, CIT2], "text", "REPORT")
    check("Misattribution check: incomplete" in rep and "1 of 2 selected" in rep, "partial verdicts: the evidence says which part is unchecked")

    n = len(pings)
    cm.select_load_bearing = lambda citations, full_text, *a, **k: []
    rep = review._run_citation_misattribution("ICSAC-SUB-99996", [UNCONFIRMED], "text", "REPORT")
    check(rep == "REPORT" and events()[-1].get("error") is None and len(pings) == n,
          "no identity-confirmed reference: a clean skip, no ping")

    cm.select_load_bearing = lambda citations, full_text, *a, **k: [CIT]
    cm.check_misattribution_batch = lambda lb, ft: []
    rep = review._run_citation_misattribution("ICSAC-SUB-TEST-1", [CIT], "text", "REPORT")
    check("could not run" in rep and len(pings) == n and events("audit-log-test.jsonl"), "test id: noted and logged, no ping")

sys.exit(1 if failures else 0)
