#!/usr/bin/env python3
"""Offline tests for intake/post_publication.py and the two notice functions in
intake/notify_author.py: the follow-up state machine, its stop conditions,
idempotence, crash recovery, tier routing, the registry lookup, and the real
renderers with only the transport (email_send.send_email) stubbed. No Gmail, no
Telegram, no network. Run from the repo root:
    .venv/bin/python intake/scripts/test_post_publication.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import intake.post_publication as pp      # noqa: E402
import intake.notify_author as na         # noqa: E402

NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
ORCID_MEMBER = "0000-0002-1825-0097"
ORCID_REVOKED = "0000-0001-5109-3700"
ORCID_SUPPORTER = "0000-0002-9079-593X"
ORCID_FREE = "0000-0003-1415-9269"
BASE = {"decision": "accept", "author_approval_status": "approved", "author_exclusions": []}


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def mk(root, sub_id, *, state, orcid=ORCID_FREE, email="author@example.com", tier=None,
       test_mode=False, offered=True, record_exclusions=None, record_status=None):
    """A submission dir. `offered` writes author_approval.json (the author saw the
    response page); its exclusions/status mirror the state unless overridden."""
    d = root / sub_id
    d.mkdir(parents=True)
    sub = {"sub_id": sub_id, "title": f"Paper {sub_id}", "source": "upload", "license": "cc-by-4.0",
           "form": {"name": "Test Author", "email": email, "orcid": orcid},
           "auth": {"orcid": orcid, "verified": True}, "creators": [{"name": "Test Author", "orcid": orcid}]}
    if tier is not None:
        sub["tier"] = tier
    if test_mode:
        sub["test_mode"] = True
    (d / "submission.json").write_text(json.dumps(sub))
    (d / "state.json").write_text(json.dumps(state))
    if offered:
        status = record_status or state.get("author_approval_status") or "pending"
        excl = record_exclusions if record_exclusions is not None else state.get("author_exclusions") or []
        (d / "author_approval.json").write_text(json.dumps({
            "token_sha256": "x" * 64, "issued_at": iso(NOW), "deadline": iso(NOW), "status": status,
            "responses": [{"at": iso(NOW), "choice": "approve", "exclusions": excl, "note": ""}]}))
    return d


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pp-test-"))
    prod = tmp / "subs"; test = prod / "test"
    prod.mkdir(); test.mkdir(); (prod / "queue").mkdir(); (test / "_outbox").mkdir()
    repo = tmp / "site"; (repo / "src" / "data").mkdir(parents=True)
    (repo / "src" / "data" / "members.yaml").write_text(
        "_counters:\n  next: 3\nmembers:\n"
        f'  "ICSAC-00001":\n    holder: "M"\n    tier: "Founding Member"\n    tier_code: FM\n    status: active\n    orcid: "{ORCID_MEMBER}"\n'
        f'  "ICSAC-00002":\n    holder: "R"\n    tier: "Reviewer"\n    tier_code: RV\n    status: revoked\n    orcid: "{ORCID_REVOKED}"\n')
    (repo / "src" / "data" / "supporters.yaml").write_text(
        f"supporters:\n- display_name: S\n  tier: supporter\n  status: active\n  orcid: https://orcid.org/{ORCID_SUPPORTER}\n")

    reg15 = iso(NOW - dt.timedelta(days=15))
    reg3 = iso(NOW - dt.timedelta(days=3))
    A = mk(prod, "ICSAC-SUB-00101", state={**BASE, "crossref_registered_at": reg15})
    B = mk(prod, "ICSAC-SUB-00102", state={**BASE, "crossref_registered_at": reg15, "author_exclusions": ["newsletter", "social"]})
    C = mk(prod, "ICSAC-SUB-00103", state={**BASE, "crossref_registered_at": reg3})
    D = mk(prod, "ICSAC-SUB-00104", state={**BASE, "crossref_registered_at": reg15, "author_approval_status": "withdrawn", "withdrawn_by_author": True})
    E = mk(prod, "ICSAC-SUB-00105", state={**BASE, "crossref_registered_at": reg15}, orcid=ORCID_MEMBER)
    F = mk(prod, "ICSAC-SUB-00106", state={**BASE, "crossref_registered_at": reg15}, orcid=ORCID_REVOKED)
    G = mk(prod, "ICSAC-SUB-00107", state={**BASE})                                    # never registered
    Hs = mk(prod, "ICSAC-SUB-00108", state={**BASE, "crossref_registered_at": reg15}, orcid=ORCID_SUPPORTER)
    I = mk(prod, "ICSAC-SUB-00109", state={**BASE, "crossref_registered_at": reg15, "author_approval_status": "hold"})
    J = mk(prod, "ICSAC-SUB-00110", state={"decision": "revise", "crossref_registered_at": reg15})
    K = mk(prod, "ICSAC-SUB-00111", state={**BASE, "crossref_registered_at": reg15}, email="")
    T3 = mk(test, "ICSAC-SUB-TEST-1700000003", state={**BASE, "crossref_registered_at": reg15}, tier=3, test_mode=True)
    T2 = mk(test, "ICSAC-SUB-TEST-1700000002", state={**BASE, "crossref_registered_at": reg15}, tier=2, test_mode=True)
    TX = mk(test, "ICSAC-SUB-TEST-1700000009", state={**BASE, "crossref_registered_at": reg15}, test_mode=True)  # no tier
    FAIL = mk(prod, "ICSAC-SUB-00112", state={**BASE, "crossref_registered_at": reg15})
    LEGACY = mk(prod, "ICSAC-SUB-00113", state={"decision": "accept", "crossref_registered_at": reg15}, offered=False)
    MISMATCH = mk(prod, "ICSAC-SUB-00114", state={**BASE, "crossref_registered_at": reg15}, record_exclusions=["newsletter"])
    RAISE = mk(prod, "ICSAC-SUB-00115", state={**BASE, "crossref_registered_at": reg15})
    STALE = mk(prod, "ICSAC-SUB-00116", state={**BASE, "crossref_registered_at": reg15,
                                               "followup_draft_started_at": iso(NOW - dt.timedelta(hours=2))})
    FRESH = mk(prod, "ICSAC-SUB-00117", state={**BASE, "crossref_registered_at": reg15,
                                               "followup_draft_started_at": iso(NOW - dt.timedelta(minutes=5))})
    CORRUPT = mk(prod, "ICSAC-SUB-00118", state={**BASE, "crossref_registered_at": reg15})
    (CORRUPT / "state.json").write_text("{not json")

    # ── stubs (transport and messaging only) ──
    pp.SUBMISSIONS_ROOT, pp.TEST_SUBMISSIONS_ROOT = prod, test
    pp.AUDIT_LOG, pp.TEST_AUDIT_LOG = tmp / "audit.jsonl", tmp / "audit-test.jsonl"
    pp.WEBSITE_REPO = str(repo)
    pings, drafts = [], []
    pp._ping = lambda msg, **kw: pings.append((msg, kw))
    real_send_followup = na.send_followup

    def fake_send_followup(**kw):
        drafts.append(kw)
        if kw["sub_id"] == FAIL.name:
            return False, "SMTP_USER or SMTP_PASSWORD not configured"
        if kw["sub_id"] == RAISE.name:
            raise na.TemplateUnfilledKeysError("unfilled keys in submission_followup.md: ['{{x}}']")
        return True, f"draft saved (tier {kw['tier']})"
    na.send_followup = fake_send_followup

    failures = []

    def check(cond, msg):
        (failures if not cond else []).append(msg)
        print(("  ok   " if cond else "  FAIL ") + msg)

    st = lambda d: json.loads((d / "state.json").read_text())  # noqa: E731

    # ── phases and visibility ──
    check(pp.phase(st(A), NOW)[0] == "due", "A registered 15 d ago is due")
    check(pp.phase(st(C), NOW)[0] == "waiting", "C registered 3 d ago is waiting")
    check(pp.phase(st(G), NOW)[0] == "not_published", "G never registered is not_published")
    due = {d.name for d in pp.due_followups(NOW)}
    check(T3.name not in due and T2.name not in due, "test subs are invisible without include_test")
    due_t = {d.name for d in pp.due_followups(NOW, include_test=True)}
    check({T3.name, T2.name, TX.name} <= due_t, "test subs are visible with include_test")
    check(CORRUPT.name not in due, "a corrupt state.json is not due and does not abort the listing")

    # ── time handling ──
    off = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    check(pp._iso(off) == "2026-10-15T16:00:00Z", "_iso converts an offset-aware time to UTC")
    check(pp._parse("2026-10-15T12:00:00") == NOW, "_parse treats a naive timestamp as UTC")

    # ── registry lookup ──
    check(pp.already_affiliated(ORCID_MEMBER)[0] == "members:ICSAC-00001", "active member ORCID is found")
    check(pp.already_affiliated(ORCID_REVOKED)[0] is None, "revoked member ORCID does not count")
    check(pp.already_affiliated(f"https://orcid.org/{ORCID_SUPPORTER}")[0] == "supporters:supporter", "supporter ORCID (URL form) is found")
    check(pp.already_affiliated(ORCID_FREE)[0] is None, "unknown ORCID is not found")
    check(pp.already_affiliated("TEST:abc")[0] is None, "test ORCID is ignored")
    for label, bad in (("missing dir", str(tmp / "nope")), ("empty setting", "")):
        try:
            pp.already_affiliated(ORCID_MEMBER, repo=bad); raised = False
        except pp.RegistryUnavailable:
            raised = True
        check(raised, f"registry {label} raises RegistryUnavailable (unknown, not 'no')")
    badrepo = tmp / "badsite"; (badrepo / "src" / "data").mkdir(parents=True)
    (badrepo / "src" / "data" / "members.yaml").write_text("members:\n- not: a mapping\n")
    (badrepo / "src" / "data" / "supporters.yaml").write_text("supporters:\n- orcid: x\n")
    try:
        pp.already_affiliated(ORCID_MEMBER, repo=str(badrepo)); raised = False
    except pp.RegistryUnavailable:
        raised = True
    check(raised, "a registry file of the wrong shape is unavailable, not empty")

    # ── author response: the record wins over the state mirror ──
    check(pp.author_response(MISMATCH)["exclusions"] == ["newsletter"], "author_approval.json exclusions win over the state mirror")
    check(pp.author_response(LEGACY)["offered"] is False, "no author_approval.json = never offered")

    # ── stay-involved paragraph (fail-closed) ──
    check(pp.wants_stay_involved(A) is True, "A gets the stay-involved paragraph")
    check(pp.wants_stay_involved(B) is False, "B (newsletter excluded) gets no paragraph")
    check(pp.wants_stay_involved(E) is False, "E (already a member) gets no paragraph")
    check(pp.wants_stay_involved(I) is False, "I (hold) gets no paragraph")
    check(pp.wants_stay_involved(D) is False, "D (withdrawn) gets no paragraph")
    check(pp.wants_stay_involved(LEGACY) is False, "legacy author (never offered the choices) gets no paragraph")
    check(pp.wants_stay_involved(MISMATCH) is False, "record-level newsletter exclusion drops the paragraph")
    check(pp.wants_stay_involved(CORRUPT) is False, "unreadable state drops the paragraph (fail-closed)")
    saved = pp.WEBSITE_REPO; pp.WEBSITE_REPO = ""
    check(pp.wants_stay_involved(A) is False, "registry unavailable drops the paragraph (fail-closed)")
    pp.WEBSITE_REPO = saved

    # ── first tick ──
    s = pp.run_tick(NOW, include_test=True, log=lambda m: None)
    check(s["drafted"] == 5, f"drafted A, F, T3, T2, TX = 5 (got {s['drafted']})")
    check(s["skipped"] == 10, f"skipped B, D, E, Hs, I, J, K, LEGACY, MISMATCH, STALE = 10 (got {s['skipped']})")
    check(s["errors"] == 3, f"FAIL + RAISE + CORRUPT counted as errors (got {s['errors']})")
    check(st(A).get("followup_drafted_at") == iso(NOW) and st(A).get("followup_draft_started_at"), "A carries the reservation and followup_drafted_at")
    check("newsletter" in st(B).get("followup_skip_reason", ""), "B skipped for the newsletter exclusion")
    check("newsletter" in st(MISMATCH).get("followup_skip_reason", ""), "MISMATCH skipped on the record's exclusion")
    check("never offered" in st(LEGACY).get("followup_skip_reason", ""), "LEGACY skipped: never offered the choices")
    check("withdrew" in st(D).get("followup_skip_reason", ""), "D skipped for withdrawal")
    check("registry" in st(E).get("followup_skip_reason", ""), "E skipped as already on the registry")
    check(st(F).get("followup_drafted_at"), "F (revoked record) still drafted")
    check("registry" in st(Hs).get("followup_skip_reason", ""), "supporter skipped")
    check("hold" in st(I).get("followup_skip_reason", ""), "hold skipped")
    check("accept" in st(J).get("followup_skip_reason", ""), "non-accept skipped")
    check("email" in st(K).get("followup_skip_reason", ""), "no email skipped")
    check("never recorded" in st(STALE).get("followup_skip_reason", ""), "stale reservation closes the record instead of drafting blind")
    check(st(FRESH).get("followup_drafted_at") is None and st(FRESH).get("followup_skipped_at") is None, "fresh reservation (5 min) is left alone")
    check(st(C).get("followup_drafted_at") is None and st(C).get("followup_skipped_at") is None, "C untouched while waiting")
    check(st(RAISE).get("followup_draft_attempts") == 1 and st(RAISE).get("followup_draft_started_at") is None, "a raising draft counts as an attempt and releases the reservation")
    check(st(FAIL).get("followup_draft_attempts") == 1 and not st(FAIL).get("followup_skipped_at"), "failed draft: attempt 1 recorded, not closed")
    tiers = {d["sub_id"]: d["tier"] for d in drafts}
    check(tiers.get(T3.name) == 3 and tiers.get(T2.name) == 2 and tiers.get(A.name) == 1, f"tier routing {tiers}")
    check(tiers.get(TX.name) == 3, "a test record without a tier is delivered the T3 way")
    check(all(d["author_name"] == "Test Author" and d["published_date"] for d in drafts), "drafts carry the author name and a published date")
    prod_pings = [p for p in pings if not p[1].get("test_mode")]
    test_pings = [p for p in pings if p[1].get("test_mode")]
    check(sum("DRAFT ready" in p[0] for p in prod_pings) == 2, "one prod ping per drafted prod paper")
    check(sum("NOT drafted" in p[0] for p in prod_pings) == 1 and "never recorded" in next(p[0] for p in prod_pings if "NOT drafted" in p[0]), "the stale reservation pinged once; ordinary skips are silent")
    check(len(test_pings) == 3 and all(p[1]["tier"] in (2, 3) for p in test_pings), "test pings carry their tier for routing")
    audit = [json.loads(l) for l in (tmp / "audit.jsonl").read_text().splitlines()]
    check(sum(1 for a in audit if a["event"] == "followup_drafted") == 2, "audit: 2 prod drafted events")
    check(sum(1 for a in audit if a["event"] == "followup_skipped") == 10, "audit: 10 skipped events")
    taudit = [json.loads(l) for l in (tmp / "audit-test.jsonl").read_text().splitlines()]
    check(all(a.get("test") for a in taudit) and len(taudit) == 3, "test audit lines are flagged test")

    # ── second tick: idempotent ──
    n_drafts, n_pings = len(drafts), len(pings)
    s2 = pp.run_tick(NOW + dt.timedelta(days=1), include_test=True, log=lambda m: None)
    check(s2["drafted"] == 0 and s2["skipped"] == 1, f"second tick drafts nothing; the only skip is the now-stale FRESH reservation ({s2})")
    check("never recorded" in st(FRESH).get("followup_skip_reason", ""), "a reservation that aged past the window closes the record")
    check(len(drafts) == n_drafts + 2, "second tick only retried the two failing drafts")
    check(st(FAIL).get("followup_draft_attempts") == 2, "failure attempt 2 recorded")
    pp.run_tick(NOW + dt.timedelta(days=2), include_test=True, log=lambda m: None)
    check(st(FAIL).get("followup_skipped_at") and "failed 3 times" in st(FAIL)["followup_skip_reason"], "third failure closes the record")
    check(sum("FAILED 3 times" in p[0] for p in pings[n_pings:]) == 2, "third failure pinged once per closed record (FAIL, RAISE)")
    s4 = pp.run_tick(NOW + dt.timedelta(days=3), include_test=True, log=lambda m: None)
    check(s4["checked"] == 0 and s4["drafted"] == 0, "nothing is due once every record is closed (the corrupt one only counts as an error)")

    # ── registry unavailable at tick time: skip + one ping ──
    U = mk(prod, "ICSAC-SUB-00119", state={**BASE, "crossref_registered_at": reg15})
    pp.WEBSITE_REPO = ""
    n_p = len(pings)
    pp.run_tick(NOW, log=lambda m: None)
    pp.WEBSITE_REPO = str(repo)
    check("could not be checked" in st(U).get("followup_skip_reason", ""), "registry unavailable at tick: skipped, not asked blind")
    check(len(pings) == n_p + 1 and "NOT drafted" in pings[-1][0], "registry unavailable: the curation team is told once")

    # ── dry run writes nothing ──
    L = mk(prod, "ICSAC-SUB-00120", state={**BASE, "crossref_registered_at": reg15})
    s5 = pp.run_tick(NOW, dry_run=True, log=lambda m: None)
    check(L.name in s5["transitions"] and st(L).get("followup_drafted_at") is None and st(L).get("followup_draft_started_at") is None, "dry run reports L as due and writes nothing")

    # ── the real renderers, transport stubbed ──
    sent = []

    def fake_send_email(**kw):
        sent.append(kw); return True, "stub"
    na.email_send.send_email = fake_send_email
    common = dict(to="a@example.com", title="T", author_name="A", published_date="October 1, 2026")
    real_send_followup(sub_id="ICSAC-SUB-00001", tier=1, **common)
    real_send_followup(sub_id="ICSAC-SUB-TEST-1", tier=2, **common)
    real_send_followup(sub_id="ICSAC-SUB-TEST-1", tier=3, **common)
    real_send_followup(sub_id="ICSAC-SUB-TEST-1", tier=1, **common)          # a test id can never go out plain
    na.send_published(to="a@example.com", sub_id="ICSAC-SUB-00001", title="T", author_name="A",
                      deposit_doi="10.67697/icsac.2026.001", deposit_url="https://doi.org/x",
                      publications_url="https://icsacinstitute.org/publications/x", stay_involved=True)
    na.send_published(to="a@example.com", sub_id="ICSAC-SUB-00001", title="T", author_name="A",
                      deposit_doi="10.67697/icsac.2026.001", deposit_url="https://doi.org/x",
                      publications_url="https://icsacinstitute.org/publications/x", stay_involved=False)
    na.send_published(to="a@example.com", sub_id="ICSAC-SUB-TEST-1", title="T", author_name="A",
                      deposit_doi="10.67697/TEST.x", deposit_url="https://doi.org/x",
                      publications_url="https://icsacinstitute.org/publications/x", tier=3)
    na.send_published(to="a@example.com", sub_id="ICSAC-SUB-TEST-1", title="T", author_name="A",
                      deposit_doi="10.67697/TEST.x", deposit_url="https://doi.org/x",
                      publications_url="https://icsacinstitute.org/publications/x")            # tier omitted
    check(len(sent) == 8, f"eight renders reached the transport ({len(sent)})")
    check(all(not k.get("send") for k in sent), "no render ever asks the transport to SEND")
    check(sent[0]["draft"] and not sent[0]["subject"].startswith("[T3"), "prod follow-up: Gmail draft, plain subject")
    check(sent[1].get("outbox_dir") and sent[1]["eml_filename"].endswith("-followup.eml"), "T2 follow-up: outbox .eml")
    check(sent[2]["draft"] and sent[2]["subject"].startswith("[T3 TEST] An invitation from ICSAC"), "T3 follow-up: prefixed draft")
    check(sent[3]["subject"].startswith("[T3 TEST]"), "a test sub id with tier 1 is still prefixed")
    check("community/donate" in sent[4]["body_md"] and "membership-affiliation" in sent[4]["body_md"], "published notice renders the two links when stay_involved")
    check("community/donate" not in sent[5]["body_md"] and "{{" not in sent[5]["body_md"], "published notice omits the paragraph when not, with no unfilled keys")
    check(sent[6]["subject"].startswith("[T3 TEST] Your ICSAC paper is live"), "T3 published notice: prefixed draft")
    check(sent[7]["subject"].startswith("[T3 TEST]"), "published notice for a test id defaults to the T3 way")
    check("about this paper unless you reply" in sent[0]["body_md"] and "works too" in sent[0]["body_md"], "follow-up copy carries the scoped silence sentence")

    print(f"\n{'PASS' if not failures else 'FAILED'}: {len(failures)} failure(s) in {tmp}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
