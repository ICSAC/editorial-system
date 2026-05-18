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

    panel_md, rqc_md = worker._scrubbed_report_pair(sub_id, title, tier=tier)

    state_path = sub_dir / "state.json"
    state_pre = json.loads(state_path.read_text()) if state_path.exists() else {}
    deposit_doi = state_pre.get("deposit_doi")
    deposit_url = state_pre.get("deposit_url")

    # DOI-route accept: register to /publications immediately (mirrors
    # submission_worker.process()). PDF-route accept defers to
    # publish_watcher after the curator publishes the draft.
    publications_url_str: str | None = None
    if verdict == "accept" and source == "doi" and not test_mode:
        try:
            publications_url_str = worker._register_doi_accept(sub_id, sub_dir, submission)
            _audit({"sub_id": sub_id, "event": "publications_registered",
                    "publications_url": publications_url_str, "by": "curator"}, test_mode=test_mode)
            print(f"  publications: registered at {publications_url_str}",
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
    if (verdict == "accept"
            and source == "upload"
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

    ok, info = notify_author.send_decision(
        to=form["email"], sub_id=sub_id, title=title,
        author_name=form["name"], verdict=verdict,
        source=source, source_ref=source_ref,
        panel_report_md=panel_md, rqc_md=rqc_md,
        deposit_doi=deposit_doi, deposit_url=deposit_url,
        publications_url=publications_url_str,
        tier=tier,
        compaction_manifest=compaction_manifest,
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
    state.update({
        "state": "completed" if ok else "completed_email_failed",
        "completed_at": _now_iso(),
        "decision": verdict,
        "decided_by": "curator",
        "decision_note": note or None,
    })
    state_path.write_text(json.dumps(state, indent=2))

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
    if worker.stub_pdf_if_doi(sub_dir, submission):
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

    print(f"applied {verdict} for {sub_id}; email_sent={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
