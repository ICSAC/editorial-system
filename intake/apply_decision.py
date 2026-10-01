"""Apply the curator's verdict to a queued ICSAC submission.

Invoked via decide.sh (or the curator-reply-channel responder) after
the curator confirms or overrides the panel's recommendation:

    decide.sh ICSAC-SUB-NNNNN <accept|revise|scope_reject> [optional note]

Runs publications registration (DOI route only), Zenodo deposit staging
(PDF route + deposit consent only), drafts the author email, appends
audit-log, clears the awaiting-decision marker, finalises state.json.

Test-tier routing (T2/T3): submissions whose sub_id matches
ICSAC-SUB-TEST-<unix-ts> are looked up under ~/icsac-submissions/test/,
their audit entries land in audit-log-test.jsonl, the email step takes
the tier kwarg from submission.json (T2 -> outbox .eml, T3 -> Gmail
draft with `[T3 TEST]` prefix), Zenodo staging passes sandbox=True for
T3 and skips entirely for T2, and the curator-ready Telegram ping
routes to TELEGRAM_TEST_CHAT_ID. Publications registration is skipped
in test mode regardless of tier (no writes to icsacinstitute.org repo).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

# Parent-package imports (editorial-system modules live at repo root, one
# level above this intake/ subpackage).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402

import notify          # noqa: E402
import publications    # noqa: E402

from . import notify_author  # local
from . import submission_worker as worker  # local — reuse _scrubbed_report_pair etc.


SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
TEST_SUBMISSIONS_ROOT = SUBMISSIONS_ROOT / "test"
AUDIT_LOG = Path(config.REVIEWS_DIR) / "audit-log.jsonl"
TEST_AUDIT_LOG = Path(config.REVIEWS_DIR) / "audit-log-test.jsonl"
SUB_ID_RE = re.compile(r"^ICSAC-SUB-(?:TEST-\d+|\d{5})$")
TEST_SUB_ID_RE = re.compile(r"^ICSAC-SUB-TEST-\d+$")
VERDICT_OK = {"accept", "revise", "scope_reject"}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _audit(event: dict, *, test_mode: bool = False) -> None:
    target = TEST_AUDIT_LOG if test_mode else AUDIT_LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as f:
        payload = {"ts": _now_iso(), **event}
        if test_mode:
            payload.setdefault("test", True)
        f.write(json.dumps(payload) + "\n")


_NAME_PARTICLES = {"de", "da", "di", "del", "della", "van", "von", "der", "den", "la", "le",
                   "du", "dos", "das", "bin", "al", "y", "e"}


def _salutation_name(name: str) -> str:
    """'[author] [author]' -> '[author] [author]', 'ada lovelace' -> 'Ada Lovelace':
    forms arrive with surnames in capitals (2026-09-28) or typed all in lower
    case (2026-09-30); the salutation should read as a name. A token already in
    mixed case is left alone; lower-case particles (de, van, ...) stay lower
    unless they open the name."""
    out = []
    for i, t in enumerate((name or "").split()):
        if t.isupper() and len(t) > 1:
            out.append(t.title())
        elif t.islower() and (i == 0 or t not in _NAME_PARTICLES):
            out.append(t[:1].upper() + t[1:])
        else:
            out.append(t)
    return " ".join(out)


def _curator_routing(test_mode: bool, tier: int) -> dict:
    """Return kwargs for notify.send_to_curator routing.

    Production: empty dict (default chat, ntfy on). Test mode at T3:
    route to TELEGRAM_TEST_CHAT_ID (if configured) and suppress ntfy
    so test runs do not trip pain alerts. Test mode at T2: suppress
    both curator pings (skip Telegram too) since T2 stays IMAP-less.
    """
    if not test_mode:
        return {}
    if tier == 3:
        chat = getattr(config, "TELEGRAM_TEST_CHAT_ID", "")
        thread = getattr(config, "TELEGRAM_TEST_THREAD_ID", "")
        if not chat:
            # No test chat configured; suppress entirely.
            return {"chat_override": "__suppress__", "ntfy": False}
        return {"chat_override": chat, "thread_override": thread,
                "ntfy": False}
    # T2 (or T1 dry-run): no curator ping.
    return {"chat_override": "__suppress__", "ntfy": False}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: apply_decision.py <sub-id> <accept|revise|scope_reject> [note]",
              file=sys.stderr)
        return 2
    sub_id = argv[1].strip()
    verdict = argv[2].strip().lower()
    note = " ".join(argv[3:]).strip() if len(argv) > 3 else ""

    if not SUB_ID_RE.match(sub_id):
        print(f"bad sub id: {sub_id!r}", file=sys.stderr)
        return 2
    if verdict not in VERDICT_OK:
        print(f"bad verdict: {verdict!r} (want accept|revise|scope_reject)",
              file=sys.stderr)
        return 2

    is_test = bool(TEST_SUB_ID_RE.match(sub_id))
    sub_root = TEST_SUBMISSIONS_ROOT if is_test else SUBMISSIONS_ROOT
    sub_dir = sub_root / sub_id
    if not sub_dir.is_dir():
        print(f"no such submission: {sub_dir}", file=sys.stderr)
        return 1

    submission = json.loads((sub_dir / "submission.json").read_text())
    force = os.environ.get("ICSAC_DECISION_FORCE") == "1"
    _state_now = sub_dir / "state.json"
    _decided = json.loads(_state_now.read_text()).get("decision") if _state_now.exists() else None
    if _decided and not force:
        # A decision is on record: a second run would draft a second email and
        # re-open the author's window (audit 2026-09-28 item 6). Checked before
        # any work is done.
        print(f"{sub_id} already has a decision on record: {_decided}. Nothing done. "
              f"To apply it again anyway (a lost draft, a corrected note) run with "
              f"ICSAC_DECISION_FORCE=1.", file=sys.stderr)
        return 3
    # tier is intake-asserted on the submission record; test_mode is
    # the canonical isolation flag. Re-derive both from the sub_id
    # prefix too as a belt-and-suspenders check against a forged
    # record file.
    tier = int(submission.get("tier") or 1)
    test_mode = bool(submission.get("test_mode")) or is_test
    if test_mode and tier == 1:
        tier = 1  # T1 short-circuits earlier in the pipeline; we should
                  # not see T1 records arrive here, but if one does treat
                  # it as a no-side-effect dry run.
    form = submission.get("form", {})
    title = submission.get("title") or "Untitled"
    source = submission.get("source") or "upload"
    source_ref = submission.get("doi") or submission.get("source_ref") or ""

    state_path = sub_dir / "state.json"
    state_pre = json.loads(state_path.read_text()) if state_path.exists() else {}

    # A paper held by the code and data check never reached the panel: it can
    # be sent back (the email asks for the link) or scope-rejected, and an
    # accept waits until code-check-override.sh has sent it to the panel.
    held_for_code_link = state_pre.get("precheck") == "code_data_link_missing"
    if held_for_code_link and verdict == "accept":
        print(f"{sub_id} was held for a missing code/data link and has no panel review. "
              f"Nothing done. If the flag was wrong, run intake/code-check-override.sh {sub_id} "
              f"and decide after the panel.", file=sys.stderr)
        return 3
    _cur_state = state_pre.get("state")
    if not force and not held_for_code_link and _cur_state != "awaiting_decision":
        # revise/scope_reject on a paper the panel has not finished would draft an
        # author email over a review that does not exist yet (audit 2026-09-29).
        print(f"{sub_id} is in state {_cur_state!r}, not awaiting a decision. Nothing done. "
              f"Decide after the panel, or run with ICSAC_DECISION_FORCE=1.", file=sys.stderr)
        return 3
    code_link_claims = None
    if held_for_code_link and verdict == "revise":
        try:
            code_link_claims = json.loads((sub_dir / "code_availability.json").read_text()).get("claims") or []
        except Exception as exc:
            print(f"  code_availability.json unreadable ({exc}); the email quotes nothing", file=sys.stderr)
            code_link_claims = []

    panel_md, rqc_md = worker._scrubbed_report_pair(sub_id, title, tier=tier)
    # The acceptance email attaches the panel report and the record publishes it:
    # an accept with none (the panel never ran, e.g. right after a code-check
    # override, or the redaction gate dropped it) waits (audit 2026-09-28).
    if verdict == "accept" and not panel_md.strip() and not force:
        print(f"{sub_id} has no publishable panel report (not reviewed yet, or the redaction "
              f"gate dropped it: see the curator alert). Nothing done. Decide after the panel, "
              f"or run with ICSAC_DECISION_FORCE=1 to accept without the report.", file=sys.stderr)
        return 3
    deposit_doi = state_pre.get("deposit_doi")
    deposit_url = state_pre.get("deposit_url")

    registrar = getattr(config, "DOI_REGISTRAR", "crossref")
    # Legacy (registrar="zenodo") DOI-route accept: register to /publications
    # immediately under the author's DOI. Under Crossref the DOI route is a
    # Persistence article like an upload (policy 2026-09-28): its own DOI,
    # linked to the preprint, published when register-doi.sh --live runs.
    publications_url_str: str | None = None
    if verdict == "accept" and source == "doi" and not test_mode and registrar != "crossref":
        try:
            publications_url_str = worker._register_doi_accept(sub_id, sub_dir, submission)
            _audit({"sub_id": sub_id, "event": "publications_registered",
                    "publications_url": publications_url_str, "by": "curator"}, test_mode=test_mode)
            print(f"  publications: registered at {publications_url_str}" if publications_url_str
                  else "  publications: HELD for the curator's approval (nothing published)",
                  file=sys.stderr)
        except Exception as exc:
            print(f"  publications register failed for {sub_id}: {exc}",
                  file=sys.stderr)
            _audit({"sub_id": sub_id, "event": "publications_register_failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:500],
                    "by": "curator"}, test_mode=test_mode)
            # Author email still sends — placeholder reads as empty string.

    # Curator-driven accept on the upload route stages a draft deposit
    # if one hasn't been staged yet (auto path catches PAUSED-then-
    # recovered cases too). DRAFT-ONLY semantics: no DOI is minted, the
    # draft waits for the curator to publish from Zenodo's UI (or
    # zenodo_deposit.publish_draft). Failure is graceful — _pending
    # template either way. Mirrors submission_worker.process().
    deposit_record_id = state_pre.get("deposit_record_id")
    skip_zenodo = test_mode and tier == 2

    # 2026-09-27: ICSAC is a Crossref member. Under registrar="crossref" an
    # accept stages a DEPOSIT DRAFT on disk (<sub_dir>/crossref/deposit.xml,
    # schema-validated, DOI string assigned under 10.67697) and stages NOTHING
    # on Zenodo. The DOI is registered only when an operator runs
    # intake/register-doi.sh --live; the accept email still says "pending".
    # Test tiers never stage (a draft is harmless, but the counter would burn).
    # T2 has no external side effects at all; T3 rehearses with a TEST DOI (no
    # counter burn) and a sandbox Zenodo draft, exactly like production.
    crossref_path = (verdict == "accept" and source in ("upload", "doi")
                     and registrar == "crossref" and not (test_mode and tier == 2))
    preprint = None
    if crossref_path:
        import crossref_deposit as _cdp
        preprint = _cdp.preprint_doi(submission)
    if crossref_path and not state_pre.get("crossref_doi"):
        try:
            import crossref_deposit
            explicit = (f"{config.CROSSREF_PREFIX}/TEST.{sub_id}" if test_mode else None)
            staged = crossref_deposit.stage(sub_dir, submission, doi=explicit,
                                            log=lambda m: print(m, file=sys.stderr))
            state_pre["crossref_doi"] = staged["doi"]
            state_pre["crossref_staged_at"] = _now_iso()
            state_pre["crossref_deposit_xml"] = staged["xml_path"]
            state_pre["crossref_landing_url"] = staged["landing_url"]
            _audit({"sub_id": sub_id, "event": "crossref_draft_staged",
                    "doi": staged["doi"], "landing_url": staged["landing_url"],
                    "by": "curator"}, test_mode=test_mode)
        except Exception as exc:
            print(f"  crossref stage failed for {sub_id}: {exc}", file=sys.stderr)
            _audit({"sub_id": sub_id, "event": "crossref_draft_failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:500],
                    "by": "curator"}, test_mode=test_mode)

    # Archival copy on Zenodo as a DRAFT that carries the ICSAC DOI as an
    # external DOI (Zenodo mints nothing). register-doi.sh --live publishes it
    # only after Crossref confirms the DOI. Same deposit_consent contract.
    # Upload route only: a DOI-route paper's preprint is already archived.
    cr_doi = state_pre.get("crossref_doi")
    if (crossref_path and source == "upload" and cr_doi and form.get("deposit_consent")
            and not state_pre.get("deposit_record_id")):
        try:
            import repository_deposit as zenodo_deposit
            # ANY test-tier submission goes to the sandbox (audit 2026-09-27 item 7: a test record
            # missing its tier field used to fall through to production Zenodo).
            sandbox = bool(test_mode)
            draft = zenodo_deposit.stage_deposit_draft(
                submission, sub_dir / "paper.pdf",
                log=lambda m: print(m, file=sys.stderr),
                sandbox=sandbox, external_doi=cr_doi,
            )
            if draft:
                state_pre["deposit_record_id"] = draft["record_id"]
                state_pre["deposit_draft_url"] = draft["draft_url"]
                _audit({"sub_id": sub_id, "event": "deposit_draft_completed",
                        "deposit_record_id": draft["record_id"],
                        "deposit_draft_url": draft["draft_url"],
                        "external_doi": cr_doi, "by": "curator"}, test_mode=test_mode)
        except Exception as exc:
            print(f"  zenodo draft (external DOI) failed for {sub_id}: {exc}", file=sys.stderr)
            _audit({"sub_id": sub_id, "event": "deposit_draft_failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:500],
                    "by": "curator"}, test_mode=test_mode)

    if crossref_path and (not test_mode or tier == 3):
        # One message with everything the curator needs to inspect before the
        # irreversible step, and the exact command that performs it.
        try:
            lines = [f"ACCEPT staged for {sub_id} -- nothing minted, nothing sent.",
                     f"Title: {title[:120]}"]
            if cr_doi:
                lines += [f"ICSAC DOI (reserved, not registered): {cr_doi}",
                          f"Crossref XML draft: {state_pre.get('crossref_deposit_xml')}",
                          f"Will resolve to: {state_pre.get('crossref_landing_url')}"]
            else:
                lines.append("Crossref draft: FAILED to stage -- see audit log")
            if preprint:
                lines.append(f"Preprint (linked as hasPreprint): https://doi.org/{preprint}")
                for flag in ((submission.get("preprint_meta") or {}).get("flags") or []):
                    lines.append(f"  check the preprint: {flag}")
            elif source == "doi":
                lines.append("Preprint: NOT RESOLVED -- check the relation before --live")
            if source == "doi":
                art_lic = (submission.get("license") or "").lower()
                if submission.get("article_license"):
                    lines.append(f"Article licence: {art_lic} (the author's choice); the preprint's: "
                                 f"{submission.get('preprint_license') or 'not reported by the server'}")
                elif not art_lic.startswith("cc"):
                    lines.append(f"Licence on the preprint: {art_lic or 'none recorded'} "
                                 f"-- check it before --live (the site hosts the PDF)")
            if source == "upload":   # a DOI-route preprint is archived where it is
                if state_pre.get("deposit_draft_url"):
                    lines.append(f"Zenodo archive DRAFT (unpublished): {state_pre['deposit_draft_url']}")
                elif form.get("deposit_consent"):
                    lines.append("Zenodo archive draft: NOT staged -- see audit log")
                else:
                    lines.append("Zenodo archive: author did not consent to deposit")
            lines += ["Acceptance email: Gmail Drafts (review and send by hand).",
                      f"When ready: intake/register-doi.sh {sub_id} --live"]
            notify.send_to_curator("\n".join(lines), parse_mode=None,
                                   **_curator_routing(test_mode, tier))
        except Exception as exc:
            print(f"  accept summary ping failed: {exc}", file=sys.stderr)

    if (verdict == "accept"
            and source == "upload"
            and registrar == "zenodo"
            and not deposit_record_id
            and form.get("deposit_consent")
            and not skip_zenodo):
        try:
            import repository_deposit as zenodo_deposit
            sandbox = test_mode and tier == 3
            label = "sandbox " if sandbox else ""
            print(f"  deposit-draft: staging {label}for {sub_id}...", file=sys.stderr)
            paper_pdf = sub_dir / "paper.pdf"
            draft = zenodo_deposit.stage_deposit_draft(
                submission, paper_pdf,
                log=lambda m: print(m, file=sys.stderr),
                sandbox=sandbox,
            )
            state_pre["deposit_record_id"] = draft["record_id"]
            state_pre["deposit_draft_url"] = draft["draft_url"]
            _audit({"sub_id": sub_id, "event": "deposit_draft_completed",
                    "deposit_record_id": draft["record_id"],
                    "deposit_draft_url": draft["draft_url"],
                    "by": "curator"}, test_mode=test_mode)
        except Exception as exc:
            print(f"  deposit-draft failed for {sub_id}: {exc}", file=sys.stderr)
            _audit({"sub_id": sub_id, "event": "deposit_draft_failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:500],
                    "by": "curator"}, test_mode=test_mode)
            # deposit_doi stays None either way — template falls back to _pending.

    # Load the blind-review compaction manifest (worker persisted it
    # alongside the submission). Missing manifest = compaction did not run
    # for this submission (older sub, or worker crash before write); the
    # disclosure block prints a graceful fallback in that case.
    compaction_manifest = None
    manifest_path = sub_dir / "compaction_manifest.json"
    if manifest_path.exists():
        try:
            compaction_manifest = json.loads(manifest_path.read_text())
        except Exception as exc:
            print(f"  manifest load failed: {exc}", file=sys.stderr)

    # Dates the author will quote back ("submitted Sunday, decided Tuesday").
    def _us(iso):
        try:
            from datetime import datetime as _dtm
            return _dtm.fromisoformat(iso.replace("Z", "+00:00")).strftime("%B %-d, %Y")
        except Exception:
            return ""
    received_date = _us(state_pre.get("received_at") or submission.get("received_at") or "")
    decided_date = _us(_now_iso())
    citation_line = ""
    try:
        import review as _review
        hdr = _review._citation_header_line(sub_id)
        if hdr:
            citation_line = ("Your reference list was checked as part of the review: "
                             + hdr.replace(" · ", "; ") + ".")
    except Exception:
        pass

    # The author's personal response link + the objection window. T2 has no
    # external surface, so no window; T3 gets one (the page shows the TEST banner).
    approval_url, objection_deadline = "", ""
    if verdict == "accept" and (source == "upload" or crossref_path) and not (test_mode and tier == 2):
        try:
            from . import author_approval
            ap = author_approval.issue(sub_dir, force=force)
            approval_url, objection_deadline = ap["url"], ap["deadline_display"]
            _audit({"sub_id": sub_id, "event": "author_window_opened",
                    "deadline": ap["deadline"], "by": "curator"}, test_mode=test_mode)
        except Exception as exc:
            # No window means no link in the email and no gate on registration:
            # stop before anything is drafted (audit 2026-09-28 item 5).
            print(f"  author approval window failed to open: {exc}; the accept was NOT applied",
                  file=sys.stderr)
            try:
                notify.send_to_curator(
                    f"ICSAC accept ABORTED for {sub_id}: the author response window could not be "
                    f"opened ({str(exc)[:160]}). Nothing drafted, nothing recorded.",
                    parse_mode=None, **_curator_routing(test_mode, tier))
            except Exception:
                pass
            return 1

    # Attribution exactly as the deposits will carry it, so the author can object
    # before the DOI is permanent.
    author_display, affiliation, license_name = "", "", ""
    try:
        import crossref_deposit as _cd
        creators = submission.get("creators") or []
        if creators:
            c0 = creators[0] if isinstance(creators[0], dict) else {"name": str(creators[0])}
            author_display = _cd.display_name(c0.get("name", ""))
            affiliation = (c0.get("affiliation") or "").strip()
            if len(creators) > 1:
                author_display += " et al."
        license_name = _cd.LICENSE_LABELS.get((submission.get("license") or "").lower(), "")
    except Exception:
        pass

    # The curation team's findings for a revise letter: <sub_dir>/curator_findings.md,
    # written by the curator before deciding (markdown, numbered points). The
    # letter's "Curation team findings" section is this file; without it the
    # draft carries a loud bracket and cannot be sent as is.
    curator_findings = ""
    findings_path = sub_dir / "curator_findings.md"
    if verdict == "revise" and findings_path.exists():
        curator_findings = findings_path.read_text().strip()
        print(f"  curator findings: {findings_path} ({len(curator_findings)} chars)", file=sys.stderr)
    elif verdict == "revise" and code_link_claims is None:
        print(f"  curator findings: {findings_path} not found; the draft carries a placeholder "
              f"bracket to fill before sending", file=sys.stderr)

    ok, info = notify_author.send_decision(
        to=form["email"], sub_id=sub_id, title=title,
        author_name=_salutation_name(form["name"]), verdict=verdict,
        source=source, source_ref=source_ref,
        panel_report_md=panel_md, rqc_md=rqc_md,
        deposit_doi=deposit_doi, deposit_url=deposit_url,
        publications_url=publications_url_str,
        tier=tier,
        compaction_manifest=compaction_manifest,
        curator_note=note or "", received_date=received_date,
        decided_date=decided_date, citation_line=citation_line,
        author_display=author_display, affiliation=affiliation, license_name=license_name,
        terms_version=submission.get("terms_version") or getattr(config, "TERMS_VERSION", ""),
        approval_url=approval_url, objection_deadline=objection_deadline,
        code_link_claims=code_link_claims, preprint_doi=preprint or "",
        exclusivity_confirmed=form.get("exclusivity_acknowledged") is True,
        curator_findings=curator_findings,
    )
    if ok:
        # Decision emails go to Gmail Drafts (curator-applied decision path).
        try:
            curator_kwargs = _curator_routing(test_mode, tier)
            notify.send_to_curator(
                f"*Draft ready*: `{sub_id}` — verdict *{verdict}* (curator)\n"
                f"To: `{form['email']}`\n"
                f"Open Gmail → Drafts to review and send.",
                parse_mode="Markdown",
                **curator_kwargs,
            )
        except Exception as exc:
            print(f"curator draft-ready ping failed: {exc}", file=sys.stderr)

    state = dict(state_pre)
    # author_approval.issue() wrote the window to disk after state_pre was read;
    # carry those two fields so the final write does not erase them (found in
    # the 2026-09-28 rehearsal: the record was right, the mirror was empty).
    on_disk = json.loads(state_path.read_text()) if state_path.exists() else {}
    for k in ("author_approval_status", "author_window_deadline"):
        if k in on_disk:
            state[k] = on_disk[k]
    state.update({
        "state": "completed" if ok else "completed_email_failed",
        "completed_at": _now_iso(),
        "decision": verdict,
        "decided_by": "curator",
        "decision_note": note or None,
    })
    _tmp = state_path.with_name(state_path.name + ".tmp")
    _tmp.write_text(json.dumps(state, indent=2))
    os.replace(_tmp, state_path)

    awaiting = sub_dir / "awaiting-decision.json"
    if awaiting.exists():
        awaiting.unlink()

    _audit({
        "sub_id": sub_id, "event": "decision_emailed",
        "verdict": verdict, "by": "curator", "note": note or None,
        "email_sent": ok,
    }, test_mode=test_mode)

    # DOI lazy-rehydration: replace local paper.pdf with a stub now that
    # the review lifecycle is closed. Bytes remain retrievable from the
    # resolver via rehydrate.sh; sha256 in the stub guards verification.
    # Upload submissions are skipped by stub_pdf_if_doi (they're the
    # archive of record).
    # A DOI-route Persistence article keeps its PDF: the site hosts the
    # version of record at registration.
    if not crossref_path and worker.stub_pdf_if_doi(sub_dir, submission):
        _audit({"sub_id": sub_id, "event": "pdf_stubbed",
                "doi": submission.get("doi", ""), "by": "curator"}, test_mode=test_mode)

    notify.send_to_curator(
        f"ICSAC decision applied\n\n"
        f"ID: {sub_id}\n"
        f"Verdict: {verdict.upper()}\n"
        f"Author email: {'sent' if ok else 'FAILED — ' + str(info)[:120]}",
        parse_mode=None,
        **_curator_routing(test_mode, tier),
    )

    print(f"applied {verdict} for {sub_id}; email_drafted={ok} "
          f"({'test outbox' if test_mode and tier == 2 else 'Gmail Drafts'} -- nothing was sent)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
