"""Apply the curator's verdict to a queued ICSAC submission.

Invoked via decide.sh (or the curator-reply-channel responder) after
the curator confirms or overrides the panel's recommendation:

    decide.sh ICSAC-SUB-NNNNN <accept|revise|scope_reject> [optional note]

Runs publications registration (DOI route only), Zenodo deposit staging
(PDF route + deposit consent only), drafts the author email, appends
audit-log, clears the awaiting-decision marker, finalises state.json.
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
AUDIT_LOG = Path(config.REVIEWS_DIR) / "audit-log.jsonl"
SUB_ID_RE = re.compile(r"^ICSAC-SUB-\d{5}$")
VERDICT_OK = {"accept", "revise", "scope_reject"}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _audit(event: dict) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps({"ts": _now_iso(), **event}) + "\n")


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

    sub_dir = SUBMISSIONS_ROOT / sub_id
    if not sub_dir.is_dir():
        print(f"no such submission: {sub_dir}", file=sys.stderr)
        return 1

    submission = json.loads((sub_dir / "submission.json").read_text())
    form = submission.get("form", {})
    title = submission.get("title") or "Untitled"
    source = submission.get("source") or "upload"
    source_ref = submission.get("doi") or submission.get("source_ref") or ""

    panel_md, rqc_md = worker._scrubbed_report_pair(sub_id, title)

    state_path = sub_dir / "state.json"
    state_pre = json.loads(state_path.read_text()) if state_path.exists() else {}
    deposit_doi = state_pre.get("deposit_doi")
    deposit_url = state_pre.get("deposit_url")

    # DOI-route accept: register to /publications immediately (mirrors
    # submission_worker.process()). PDF-route accept defers to
    # publish_watcher after the curator publishes the draft.
    publications_url_str: str | None = None
    if verdict == "accept" and source == "doi":
        try:
            publications_url_str = worker._register_doi_accept(sub_id, sub_dir, submission)
            _audit({"sub_id": sub_id, "event": "publications_registered",
                    "publications_url": publications_url_str, "by": "curator"})
            print(f"  publications: registered at {publications_url_str}",
                  file=sys.stderr)
        except Exception as exc:
            print(f"  publications register failed for {sub_id}: {exc}",
                  file=sys.stderr)
            _audit({"sub_id": sub_id, "event": "publications_register_failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:500],
                    "by": "curator"})
            # Author email still sends — placeholder reads as empty string.

    # Curator-driven accept on the upload route stages a draft deposit
    # if one hasn't been staged yet (auto path catches PAUSED-then-
    # recovered cases too). DRAFT-ONLY semantics: no DOI is minted, the
    # draft waits for the curator to publish from Zenodo's UI (or
    # zenodo_deposit.publish_draft). Failure is graceful — _pending
    # template either way. Mirrors submission_worker.process().
    deposit_record_id = state_pre.get("deposit_record_id")
    if (verdict == "accept"
            and source == "upload"
            and not deposit_record_id
            and form.get("deposit_consent")):
        try:
            import repository_deposit as zenodo_deposit
            print(f"  deposit-draft: staging for {sub_id}...", file=sys.stderr)
            paper_pdf = sub_dir / "paper.pdf"
            draft = zenodo_deposit.stage_deposit_draft(
                submission, paper_pdf,
                log=lambda m: print(m, file=sys.stderr),
            )
            state_pre["deposit_record_id"] = draft["record_id"]
            state_pre["deposit_draft_url"] = draft["draft_url"]
            _audit({"sub_id": sub_id, "event": "deposit_draft_completed",
                    "deposit_record_id": draft["record_id"],
                    "deposit_draft_url": draft["draft_url"],
                    "by": "curator"})
        except Exception as exc:
            print(f"  deposit-draft failed for {sub_id}: {exc}", file=sys.stderr)
            _audit({"sub_id": sub_id, "event": "deposit_draft_failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:500],
                    "by": "curator"})
            # deposit_doi stays None either way — template falls back to _pending.

    ok, info = notify_author.send_decision(
        to=form["email"], sub_id=sub_id, title=title,
        author_name=form["name"], verdict=verdict,
        source=source, source_ref=source_ref,
        panel_report_md=panel_md, rqc_md=rqc_md,
        deposit_doi=deposit_doi, deposit_url=deposit_url,
        publications_url=publications_url_str,
    )
    if ok:
        # Decision emails go to Gmail Drafts (curator-applied decision path).
        try:
            notify.send_to_curator(
                f"*Draft ready*: `{sub_id}` — verdict *{verdict}* (curator)\n"
                f"To: `{form['email']}`\n"
                f"Open Gmail → Drafts to review and send.",
                parse_mode="Markdown",
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
    })

    # DOI lazy-rehydration: replace local paper.pdf with a stub now that
    # the review lifecycle is closed. Bytes remain retrievable from the
    # resolver via rehydrate.sh; sha256 in the stub guards verification.
    # Upload submissions are skipped by stub_pdf_if_doi (they're the
    # archive of record).
    if worker.stub_pdf_if_doi(sub_dir, submission):
        _audit({"sub_id": sub_id, "event": "pdf_stubbed",
                "doi": submission.get("doi", ""), "by": "curator"})

    notify.send_to_curator(
        f"ICSAC decision applied\n\n"
        f"ID: {sub_id}\n"
        f"Verdict: {verdict.upper()}\n"
        f"Author email: {'sent' if ok else 'FAILED — ' + str(info)[:120]}",
        parse_mode=None,
    )

    print(f"applied {verdict} for {sub_id}; email_sent={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
