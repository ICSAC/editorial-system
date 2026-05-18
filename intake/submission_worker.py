"""ICSAC submission worker — drains queue/, runs the panel, dispatches.

Triggered by submission-worker.path (systemd) when a marker appears in
~/icsac-submissions/queue/. Processes markers one at a time (the .service
unit is single-instance), calls review.review_paper(), and routes every
recommendation to the curator's configured reply channel for the final
verdict.

Recommendation handling:
  PAUSED_AI_FAILURE  → curator alert only (panel already fired its alert);
                       no author email yet. Manual rerun via worker re-trigger.
  ANY OTHER          → escalate to curator with the panel's recommendation
                       as a starting point. The curator drives the final
                       verdict via the configured reply channel;
                       apply_decision.py runs publications registration,
                       Zenodo deposit staging, and the author email after
                       the curator confirms.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Parent-package imports (editorial-system modules live at repo root, one
# level above this intake/ subpackage).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config        # noqa: E402
import submission_intake as ingest        # noqa: E402
import publications  # noqa: E402
import review        # noqa: E402
import redaction      # noqa: E402
import notify        # noqa: E402

from . import notify_author  # local
from .time_fmt import now_et_display  # local


SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
QUEUE_DIR = SUBMISSIONS_ROOT / "queue"
TEST_SUBMISSIONS_ROOT = SUBMISSIONS_ROOT / "test"
TEST_QUEUE_DIR = TEST_SUBMISSIONS_ROOT / "queue"
AUDIT_LOG = Path(config.REVIEWS_DIR) / "audit-log.jsonl"
TEST_AUDIT_LOG = Path(config.REVIEWS_DIR) / "audit-log-test.jsonl"
TEST_REVIEWS_DIR = Path(config.REVIEWS_DIR) / "test"
INTAKE_DIR = Path(__file__).parent


def _log(msg: str, *, err: bool = False) -> None:
    """Worker stdout/stderr line with an ET timestamp prefix.

    journalctl already pins each line to a system clock timestamp, but
    that's the host's local time and the format isn't what curators
    asked for. Prefix every lifecycle message with [MM/DD/YY HH:MM:SS
    EDT|EST] so the log line carries the ET wall-clock the curator
    expects, regardless of how the system clock is presented.
    """
    line = f"[{now_et_display()}] {msg}"
    print(line, file=(sys.stderr if err else sys.stdout))

# Where the incident-router watches for active incidents. Worker writes
# incident JSON over SSH; the router picks it up on its poll loop. Both
# values default to empty so a forked deployment without a router simply
# short-circuits the SSH step (see _write_incident_to_remote).
INCIDENT_SSH_HOST = os.environ.get("INCIDENT_SSH_HOST", "")
INCIDENT_REMOTE_DIR = os.environ.get("INCIDENT_REMOTE_DIR", "")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _audit(event: dict, *, test_mode: bool = False) -> None:
    """Append a JSONL audit entry. Test-mode entries route to the
    sibling audit-log-test.jsonl so production observability never
    sees them — same isolation contract as intake_server._audit_append.
    """
    target = TEST_AUDIT_LOG if test_mode else AUDIT_LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    base = {"ts": _now_iso()}
    if test_mode:
        base["test"] = True
    entry = {**base, **event}
    with target.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _resolve_sub_dir(sub_id: str) -> tuple[Path, int]:
    """Return (sub_dir, tier) for `sub_id`. Production submissions live
    under SUBMISSIONS_ROOT/<id> and have tier=1 (with no `tier` field on
    submission.json); test-tier submissions live under
    SUBMISSIONS_ROOT/test/<id> with `tier` ∈ {2, 3} explicitly set.
    """
    test_dir = TEST_SUBMISSIONS_ROOT / sub_id
    prod_dir = SUBMISSIONS_ROOT / sub_id
    if test_dir.is_dir():
        try:
            sub = json.loads((test_dir / "submission.json").read_text())
        except Exception:
            sub = {}
        tier = int(sub.get("tier") or 1)
        return test_dir, tier
    return prod_dir, 1


def _write_state(sub_dir: Path, **fields) -> dict:
    state_path = sub_dir / "state.json"
    data = json.loads(state_path.read_text()) if state_path.exists() else {}
    data.update(fields)
    state_path.write_text(json.dumps(data, indent=2))
    return data


def _resolve_pending_doi(sub_id: str, sub_dir: Path,
                         submission: dict) -> tuple[bool, str]:
    """Worker-side DOI resolution. Runs at the start of process() when
    submission.pending_pdf_fetch is True (i.e. handler deferred the
    PDF fetch). Returns (ok, error_reason). On success, paper.pdf
    exists in sub_dir and submission.json has been updated with
    resolved metadata + pending_pdf_fetch cleared.

    On failure, the caller is expected to email the author the
    failure reason via notify_author.send_intake_failure and write
    a terminal state on state.json. We don't email from here so the
    caller can audit + log + notify atomically.
    """
    doi = submission.get("doi") or submission.get("source_ref") or ""
    if not doi:
        return (False, "no DOI on submission record")

    # Dispatch on shape — arXiv refs go through fetch_arxiv_metadata +
    # download_arxiv_pdf; everything else is treated as Zenodo (the only
    # other supported source). Crossref/DataCite generic resolution is a
    # known gap, surfaced at handler-time via _validate_doi_shape.
    if ingest.is_arxiv_ref(doi):
        arxiv_id = ingest.arxiv_ref_to_id(doi)
        try:
            review_meta = ingest.fetch_arxiv_metadata(arxiv_id)
        except Exception as exc:
            # Log full detail (with library exception) to journal; pass a
            # sanitized one-liner to the author so transport errors don't
            # leak as raw stack-trace fragments in the outbound email.
            _log(f"  arXiv metadata fetch failed for {doi}: "
                 f"{type(exc).__name__}: {exc}", err=True)
            return (False, f"could not fetch arXiv manifest for {doi}; please retry shortly")
        download_path = ingest.download_arxiv_pdf(arxiv_id, dest_dir=str(sub_dir))
        if not download_path:
            return (False, f"the arXiv record {arxiv_id} could not be downloaded as PDF")
    else:
        record_id = ingest.doi_to_record_id(doi)
        try:
            metadata = ingest.fetch_metadata(record_id)
        except Exception as exc:
            _log(f"  Zenodo metadata fetch failed for {doi}: "
                 f"{type(exc).__name__}: {exc}", err=True)
            return (False, f"could not fetch DOI manifest for {doi}; please retry shortly")
        download_path = ingest.download_pdf(metadata, dest_dir=str(sub_dir))
        if not download_path:
            return (False, f"the Zenodo record at {doi} does not include a PDF file")
        review_meta = ingest.extract_review_data(metadata)

    # Canonicalize PDF location. Both ingest.download_arxiv_pdf and
    # ingest.download_pdf save under their own naming conventions
    # (`<arxiv_id>.pdf`, `<record_id>_<key>.pdf`); downstream code
    # (_build_review_data, lazy-rehydration stub, audit) all expect
    # `<sub_dir>/paper.pdf`. Move into place; if a file already exists
    # at the canonical path (rerun scenario), unlink it first.
    paper_path = sub_dir / "paper.pdf"
    src = Path(download_path)
    if src.resolve() != paper_path.resolve():
        if paper_path.exists():
            paper_path.unlink()
        src.rename(paper_path)

    keywords = review_meta.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = [str(keywords)]
    license_id = ""
    lic = review_meta.get("license") or {}
    if isinstance(lic, dict):
        license_id = (lic.get("id") or "").lower()
    creators = review_meta.get("creators") or submission.get("creators") or [
        submission.get("form", {}).get("name", "Unknown")
    ]
    pdf_size = paper_path.stat().st_size
    pdf_sha = hashlib.sha256(paper_path.read_bytes()).hexdigest()

    submission.update({
        "pending_pdf_fetch": False,
        "title": review_meta.get("title") or "Untitled",
        "abstract": review_meta.get("description") or "",
        "keywords": keywords,
        "license": license_id,
        "creators": creators,
        "publication_date": review_meta.get("publication_date")
            or submission.get("publication_date") or "",
        "pdf": {
            "filename": "paper.pdf",
            "size_bytes": pdf_size,
            "sha256": pdf_sha,
        },
    })
    (sub_dir / "submission.json").write_text(json.dumps(submission, indent=2))
    return (True, "")


def _fail_intake(sub_id: str, sub_dir: Path, submission: dict,
                 reason: str, remediation: str,
                 state_label: str) -> None:
    """Terminal-fail an intake before review begins. Writes state, audits,
    and emails the author. Used for DOI-fetch and text-layer failures
    that the worker only discovers post-handler-202."""
    form = submission.get("form", {})
    _write_state(sub_dir, state=state_label,
                 completed_at=_now_iso(), decision=state_label,
                 failure_reason=reason)
    _audit({"sub_id": sub_id, "event": "intake_failed",
            "reason": reason[:300]})
    try:
        notify_author.send_intake_failure(
            to=form.get("email", ""),
            sub_id=sub_id,
            author_name=form.get("name", "Researcher"),
            failure_reason=reason,
            remediation=remediation,
        )
    except Exception as exc:
        _audit({"sub_id": sub_id, "event": "intake_failure_email_failed",
                "error": str(exc)[:200]})
    _alert_remote(
        f"ICSAC intake failed: {state_label}",
        f"{sub_id}: {reason}",
    )
    notify.send_to_curator(
        f"ICSAC intake failed\n\nID: {sub_id}\n"
        f"Reason: {reason}\nState: {state_label}",
        parse_mode=None,
    )


def _build_review_data(sub_id: str, sub_dir: Path) -> dict:
    """Synthesize the review_data dict that review.review_paper() expects.
    Reads the submission.json schema produced by intake_server.py — title,
    abstract, etc. live at the top level (DOI-resolved or upload-supplied);
    the `form` block holds submitter identity only."""
    submission = json.loads((sub_dir / "submission.json").read_text())
    form = submission.get("form", {})
    pdf_path = str(sub_dir / "paper.pdf")
    full_text = ingest.extract_pdf_text(pdf_path)
    license_id = (submission.get("license") or "").strip()
    creators = submission.get("creators") or [form.get("name", "Unknown")]

    return {
        "record_id": sub_id,
        "doi": submission.get("doi") or "",
        "title": submission.get("title") or "Untitled",
        "creators": creators,
        "description": submission.get("abstract") or "",
        "keywords": submission.get("keywords") or [],
        "publication_date": submission.get("publication_date")
            or submission["received_at"][:10],
        "resource_type": {"type": "publication", "subtype": "preprint"},
        "license": {"id": license_id} if license_id else {},
        "related_identifiers": [],
        "version": "1",
        "pdf_path": pdf_path,
        "full_text": full_text,
        "raw_metadata": {
            "submission": {
                "sub_id": sub_id,
                "received_at": submission["received_at"],
                "source": submission.get("source"),
                "source_ref": submission.get("source_ref"),
                "submitter_orcid": form.get("orcid"),
                "submitter_email": form.get("email"),
            }
        },
    }


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower())[:50]


def _scrubbed_report_pair(sub_id: str, title: str,
                          *, tier: int = 1) -> tuple[str, str]:
    """Read the saved review markdown + RQC, redact them, return as a
    (panel_md, rqc_md) tuple ready for PDF rendering and attachment.

    Each component independently bails with an empty string and an alert
    if its redaction gate trips — the worker proceeds (state still written),
    but the corresponding PDF attachment is skipped instead of leaking
    vendor/model identifiers or the RQC injection_indicators dim.

    For tiers 2/3 the markdown lives under reviews/test/ (the worker
    relocates it post-panel-write); we look there first and fall back to
    the production reviews dir so manual reruns under either path
    continue to work.
    """
    base_dir = TEST_REVIEWS_DIR if tier in (2, 3) else Path(config.REVIEWS_DIR)
    review_md_path = base_dir / f"{sub_id}_{_slug(title)}.md"
    if not review_md_path.exists():
        # Fallback to the production reviews dir — covers re-runs where
        # the markdown was not relocated, or production submissions
        # processed via the same helper from a non-process() entry point.
        review_md_path = (
            Path(config.REVIEWS_DIR) / f"{sub_id}_{_slug(title)}.md"
        )
    public_md = ""
    if review_md_path.exists():
        try:
            parsed = redaction.parse_review_file(str(review_md_path))
            public_md = redaction.build_public_markdown(parsed)
            redaction.assert_clean(public_md, artifact_path=str(review_md_path))
        except Exception as exc:
            print(f"  redaction failed for {sub_id}: {exc}", file=sys.stderr)
            _alert_remote(
                "ICSAC submission redaction gate tripped",
                f"{sub_id}: redacted review failed assert_clean — {exc}. "
                "Author email PDF attachment dropped; raw review on disk "
                f"at {review_md_path}",
            )
            public_md = ""

    rqc_md_path = base_dir / f"{sub_id}_review_quality_control.md"
    if not rqc_md_path.exists():
        rqc_md_path = (
            Path(config.REVIEWS_DIR) / f"{sub_id}_review_quality_control.md"
        )
    public_rqc = ""
    if rqc_md_path.exists():
        try:
            parsed_rqc = redaction.parse_rqc_file(str(rqc_md_path))
            public_rqc = redaction.build_public_rqc_markdown(parsed_rqc)
            redaction.assert_rqc_clean(public_rqc,
                                       artifact_path=str(rqc_md_path))
        except Exception as exc:
            print(f"  RQC redaction failed for {sub_id}: {exc}", file=sys.stderr)
            _alert_remote(
                "ICSAC submission RQC redaction gate tripped",
                f"{sub_id}: RQC failed assert_rqc_clean — {exc}",
            )
            public_rqc = ""

    return public_md, public_rqc


def _alert_remote(title: str, message: str) -> None:
    """Fire an out-of-band alert to the curator's configured channel.

    Both the destination URL and the per-host prefix come from env vars
    and default to empty; a deployment that hasn't configured an alert
    channel simply gets a no-op. We short-circuit before constructing
    the request so a missing webhook never touches the network.
    """
    webhook = os.environ.get("ALERT_WEBHOOK_URL", "")
    if not webhook:
        return
    import urllib.request
    node = os.environ.get("INTAKE_NODE_NAME", "intake")
    try:
        req = urllib.request.Request(webhook, data=message.encode())
        req.add_header("Title", f"{node}: {title}")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _write_incident_to_remote(incident: dict) -> bool:
    """SCP-style write: pipe JSON over SSH into <INCIDENT_REMOTE_DIR>/<fp>.json
    so a remote incident-router picks it up. Returns True on success;
    short-circuits to False (without raising) when either env var is empty
    so a forked deployment without a router simply skips the handoff."""
    if not INCIDENT_SSH_HOST or not INCIDENT_REMOTE_DIR:
        print("  remote incident handoff skipped — "
              "INCIDENT_SSH_HOST / INCIDENT_REMOTE_DIR not configured",
              file=sys.stderr)
        return False
    fp = incident["fingerprint"]
    target = f"{INCIDENT_REMOTE_DIR}/{fp}.json"
    payload = json.dumps(incident, indent=2)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             INCIDENT_SSH_HOST,
             f"mkdir -p {INCIDENT_REMOTE_DIR} && cat > {target}"],
            input=payload, text=True, capture_output=True, timeout=20,
        )
        return result.returncode == 0
    except Exception as exc:
        print(f"  remote incident handoff failed: {exc}", file=sys.stderr)
        return False


def _escalate_for_decision(sub_id: str, sub_dir: Path,
                           review_data: dict, aggregate: dict,
                           *, chat_override: str | None = None,
                           thread_override: str | None = None) -> None:
    """Open a conversation with the curator via the configured reply
    channel and hand off the incident to the incident-router so the
    curator's reply can drive `decide.sh <verdict>`.

    Called for every panel recommendation except PAUSED_AI_FAILURE.
    The panel produces a recommendation; the curator decides the
    verdict. REVIEW_FURTHER specifically flags low panel confidence;
    RECOMMEND / REJECT / REVISE_AND_RESUBMIT are starting points the
    curator is free to confirm or override.
    """
    title = review_data["title"]
    rec = aggregate.get("recommendation", "REVIEW_FURTHER")
    avg = aggregate.get("average_score")
    dim_scores = aggregate.get("dimension_scores", {})
    score_lines = []
    for dim, payload in dim_scores.items():
        mean = payload.get("mean") if isinstance(payload, dict) else payload
        score_lines.append(f"  {dim}: {mean}")
    score_block = "\n".join(score_lines) if score_lines else "  (no per-dim scores)"

    rqc_path = Path(config.REVIEWS_DIR) / f"{sub_id}_review_quality_control.md"
    rqc_flag = "?"
    if rqc_path.exists():
        match = re.search(
            r"review_quality_control_flag:\s*(true|false)",
            rqc_path.read_text(),
        )
        if match:
            rqc_flag = match.group(1)

    fingerprint = "icsacsub" + hashlib.sha1(sub_id.encode()).hexdigest()[:10]
    initial_msg = (
        f"ICSAC submission — needs your call.\n\n"
        f"ID: {sub_id}\n"
        f"Title: {title[:200]}\n"
        f"Panel recommendation: {rec}\n"
        f"Aggregate score: {avg if avg is not None else '(n/a)'}\n"
        f"Disagreement: {'yes' if aggregate.get('disagreement', False) else 'no'}\n"
        f"RQC flag: {rqc_flag}\n\n"
        f"Per-dimension means:\n{score_block}\n\n"
        f"Your call: accept / revise / scope_reject\n"
        f"(Reply on the curator's configured reply channel; "
        f"or 'park' to shelve until later.)"
    )
    msg_id = notify.send_telegram(initial_msg, parse_mode=None,
                                  chat_override=chat_override,
                                  thread_override=thread_override)
    bot_msg_ids = []
    if isinstance(msg_id, int):
        bot_msg_ids.append(msg_id)

    incident = {
        "fingerprint": fingerprint,
        "incident_type": "icsac_submission",
        "sub_id": sub_id,
        "signal_title": f"ICSAC {rec}: {title[:80]}",
        "signal_msg": initial_msg,
        "analysis": (
            f"Panel returned {rec}. The curator's reply "
            "(accept/revise/scope_reject) maps to a single ACTION line of "
            f"the form {INTAKE_DIR / 'decide.sh'} {sub_id} <verdict>"
        ),
        "rating": "NEEDS_HUMAN",
        "target_name": os.environ.get("INTAKE_NODE_NAME", "intake")
                       + " (icsac-submission-intake)",
        "target_ssh": os.environ.get("INTAKE_NODE_SSH", ""),
        "telegram_message_id": bot_msg_ids[0] if bot_msg_ids else None,
        "bot_message_ids": bot_msg_ids,
        "signal_time": _now_iso(),
        "created": _now_iso(),
        "last_activity": _now_iso(),
        "conversation": [
            {"role": "assistant", "content": initial_msg},
        ],
        "status": "active",
    }
    handed_off = _write_incident_to_remote(incident)
    if not handed_off:
        _alert_remote(
            "ICSAC curator handoff failed",
            f"{sub_id}: could not write incident to the remote "
            "incident-router. The curator was paged but the responder has "
            "no conversation context — reply with an explicit verdict word "
            "so the responder can match the incident by most-recent fallback.",
        )

    # Persist locally too so apply_decision can reconstruct without SSH.
    (sub_dir / "awaiting-decision.json").write_text(json.dumps({
        "sub_id": sub_id,
        "fingerprint": fingerprint,
        "panel_recommendation": rec,
        "telegram_message_id": incident["telegram_message_id"],
        "opened_at": _now_iso(),
    }, indent=2))


def stub_pdf_if_doi(sub_dir: Path, submission: dict) -> bool:
    """If this is a DOI submission and paper.pdf still exists, replace it
    with a paper.pdf.url stub. The bytes are identically retrievable from
    the resolver via rehydrate.sh; sha256 + size_bytes preserve verification.

    Upload submissions are skipped — the local PDF IS the archive of record
    and must not be replaced. Returns True if a stub was written, False if
    the file was kept.
    """
    if (submission.get("source") or "").lower() != "doi":
        return False
    paper = sub_dir / "paper.pdf"
    if not paper.exists():
        return False
    pdf_meta = submission.get("pdf") or {}
    sha = pdf_meta.get("sha256") or ""
    size = pdf_meta.get("size_bytes") or 0
    doi = submission.get("doi") or ""
    if not (sha and doi):
        # Missing the metadata that makes rehydration verifiable. Don't
        # delete the bytes if we can't prove we can get them back.
        return False
    stub_payload = {
        "kind": "doi-lazy-rehydration-stub",
        "doi": doi,
        "sha256": sha,
        "size_bytes": size,
        "stubbed_at": _now_iso(),
        "rehydrate_with": (
            f"{INTAKE_DIR / 'rehydrate.sh'} "
            f"{submission.get('sub_id', '')}"
        ),
    }
    (sub_dir / "paper.pdf.url").write_text(
        json.dumps(stub_payload, indent=2) + "\n"
    )
    paper.unlink()
    return True


def _email_decision(sub_id: str, sub_dir: Path, verdict: str,
                    review_data: dict,
                    publications_url: str | None = None,
                    tier: int = 1) -> bool:
    """Send the appropriate result email. Returns True on success.

    The panel report and RQC audit ride as PDF attachments named
    icsac-review-<sub_id>.pdf and icsac-rqc-<sub_id>.pdf rather than
    inlined into the body. send_decision picks the per-source template
    variant from submission.source ∈ {doi, upload}.

    `tier` controls the delivery destination per the test-tier matrix:
    tier 1 → Gmail Drafts (production); tier 2 → outbox .eml file (no
    Gmail interaction); tier 3 → Gmail Drafts with `[T3 TEST] ` subject
    prefix. The curator draft-ready ping is suppressed for tiers 2 and 3.
    """
    title = review_data["title"]
    submission = json.loads((sub_dir / "submission.json").read_text())
    form = submission.get("form", {})
    source = submission.get("source") or "upload"
    source_ref = submission.get("doi") or submission.get("source_ref") or ""

    panel_md, rqc_md = _scrubbed_report_pair(sub_id, title, tier=tier)

    state_path = sub_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    deposit_doi = state.get("deposit_doi")
    deposit_url = state.get("deposit_url")

    ok, _msg = notify_author.send_decision(
        to=form["email"], sub_id=sub_id, title=title,
        author_name=form["name"], verdict=verdict,
        source=source, source_ref=source_ref,
        panel_report_md=panel_md, rqc_md=rqc_md,
        deposit_doi=deposit_doi, deposit_url=deposit_url,
        publications_url=publications_url,
        tier=tier,
    )
    if ok and tier == 1:
        # Decision emails go to Gmail Drafts (per 2026-05-07 policy).
        # Ping curator so they know to open Gmail, review, and send.
        try:
            notify.send_to_curator(
                f"*Draft ready*: `{sub_id}` — verdict *{verdict}*\n"
                f"To: `{form['email']}`\n"
                f"Open Gmail → Drafts to review and send.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            print(f"curator draft-ready ping failed: {exc}", file=sys.stderr)
    return bool(ok)


def _bare_doi(s: str) -> str:
    """Strip a https://doi.org/ prefix to get the canonical DOI string."""
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s or "").strip()


def _zenodo_record_id_from_doi(doi: str) -> str | None:
    """Return the Zenodo numeric record id if `doi` is a Zenodo DOI."""
    m = re.match(r"^10\.5281/zenodo\.(\d+)$", _bare_doi(doi))
    return m.group(1) if m else None


def _proto_authors(submission: dict) -> list[str]:
    """Extract a flat list of author display names from submission.json.

    Prefers the `creators` array (post-Tier-2 metadata expansion); falls
    back to the form-supplied submitter name when creators is empty.
    """
    out: list[str] = []
    for c in submission.get("creators") or []:
        if isinstance(c, dict):
            name = (c.get("name") or "").strip()
        elif isinstance(c, str):
            name = c.strip()
        else:
            name = ""
        if "," in name:
            last, after = [s.strip() for s in name.split(",", 1)]
            name = f"{after} {last}".strip() if after else last
        if name:
            out.append(name)
    if not out:
        n = (submission.get("form", {}).get("name") or "").strip()
        out = [n or "Unknown"]
    return out


def _register_doi_accept(sub_id: str, sub_dir: Path,
                         submission: dict) -> str:
    """Register a DOI-route accept to the publications registry, stage the
    redacted review under <slug>.{md,html}, commit, push.

    Returns the canonical https://icsacinstitute.org/publications/<slug>
    URL the accept email points the author at.

    DOI-route only — PDF route defers to publish_watcher.poll_drafts after
    the curator publishes the staged Zenodo draft.
    """
    doi = _bare_doi(submission.get("doi") or submission.get("source_ref") or "")
    if not doi:
        raise ValueError("DOI-route submission missing doi field")

    title = submission.get("title") or "Untitled"
    abstract = submission.get("abstract") or ""

    proto = {
        "title": title,
        "authors": _proto_authors(submission),
        "doi": doi,
        "abstract": abstract[:2000],
        "source": "submission-doi",
        "source_ref": f"https://doi.org/{doi}",
    }
    record_id = _zenodo_record_id_from_doi(doi)
    if record_id:
        proto["record_id"] = record_id

    entry = publications.upsert_entry(proto)
    slug = entry["slug"]

    review_md, rqc_md = publications.stage_public_review_for_slug(
        sub_id, slug, config.REVIEWS_DIR,
    )

    publications.commit_and_push(
        message=f"publications: {entry['title']} ({slug})",
        extra_paths=[review_md, rqc_md],
    )
    return publications.publications_url(slug)


def process(sub_id: str) -> None:
    sub_dir, tier = _resolve_sub_dir(sub_id)
    if not sub_dir.is_dir():
        print(f"  no such submission dir: {sub_dir}", file=sys.stderr)
        return

    test_mode = tier in (2, 3)
    if test_mode:
        _log(f"=== Processing {sub_id} (T{tier} test pipeline) ===")
    else:
        _log(f"=== Processing {sub_id} ===")

    # Local audit + alert helpers so every entry written from this
    # process call routes to the right log + skips curator pings on
    # the test path. Side-effect routing (Zenodo, curator alerts,
    # registry writes) gates on `test_mode` further down.
    def _a(entry: dict) -> None:
        _audit(entry, test_mode=test_mode)

    def _maybe_alert(title: str, message: str) -> None:
        if test_mode:
            _log(f"  alert suppressed (T{tier}): {title} — {message}")
        else:
            _alert_remote(title, message)

    submission = json.loads((sub_dir / "submission.json").read_text())

    # Deferred-DOI resolution. Handler returned 202 immediately for DOI
    # submissions; we do the actual Zenodo fetch + PDF download here so
    # the request stays under sub-second.
    if submission.get("pending_pdf_fetch"):
        _log(f"  resolving deferred DOI {submission.get('doi')}...")
        _write_state(sub_dir, state="resolving_doi",
                     resolving_at=_now_iso())
        _a({"sub_id": sub_id, "event": "doi_resolution_started",
            "doi": submission.get("doi", "")})
        ok, reason = _resolve_pending_doi(sub_id, sub_dir, submission)
        if not ok:
            _log(f"  DOI resolution failed: {reason}", err=True)
            _fail_intake(
                sub_id, sub_dir, submission,
                reason=reason,
                remediation=(
                    "If the DOI is correct and the record is public, the "
                    "fetch may have failed transiently — resubmit. If the "
                    "record genuinely has no PDF, deposit the manuscript "
                    "PDF on Zenodo first, or use the 'Upload PDF' tab on "
                    "the submission form to upload it directly."
                ),
                state_label="rejected_doi_unresolvable",
            )
            return
        _a({"sub_id": sub_id, "event": "doi_resolved",
            "title": submission.get("title", "")[:200],
            "pdf_size_bytes": submission.get("pdf", {}).get("size_bytes", 0),
            "pdf_sha256": submission.get("pdf", {}).get("sha256", "")})

        # Now that the title is real (resolved from Zenodo/arXiv metadata),
        # fire the deferred "received" email. The intake handler skipped
        # this on DOI-route to avoid sending a confirmation with the
        # placeholder "(deferred — resolving from DOI)" in the body.
        # On the test path we skip the received-email entirely — the
        # author email contract for T2/T3 is the DECISION email; we don't
        # send acknowledgements to a real recipient from a test run.
        if not test_mode:
            form = submission.get("form", {})
            try:
                notify_author.send_received(
                    to=form["email"], sub_id=sub_id,
                    title=submission.get("title") or "(untitled)",
                    author_name=form["name"],
                )
            except Exception as exc:
                _log(f"  received email failed (non-fatal): {exc}", err=True)
                _a({"sub_id": sub_id, "event": "received_email_failed",
                    "error": str(exc)[:200]})

    _write_state(sub_dir, state="in_review",
                 review_started_at=_now_iso())
    _a({"sub_id": sub_id, "event": "review_started"})

    review_data = _build_review_data(sub_id, sub_dir)

    # Text-layer check. Handler enforced this for upload mode; for DOI
    # mode the PDF was just downloaded by _resolve_pending_doi so this
    # is the first time it's been validated. Fail-and-email is the same
    # for both — author gets a specific remediation message.
    full_text_len = len(review_data.get("full_text", ""))
    if full_text_len < ingest.PDF_TEXT_MIN_CHARS:
        _fail_intake(
            sub_id, sub_dir, submission,
            reason=(
                f"the PDF for this submission has no extractable text layer "
                f"({full_text_len} characters extracted). ICSAC reviews "
                f"text-layer PDFs only; image-only scans and raster prints "
                f"cannot be evaluated by the panel"
            ),
            remediation=(
                "Re-export your manuscript with a text layer (most modern "
                "word processors and LaTeX toolchains do this by default — "
                "the issue is usually a print-to-PDF or scan workflow), "
                "and resubmit either the new DOI or the new PDF directly."
            ),
            state_label="rejected_no_text_layer",
        )
        return

    try:
        markdown, aggregate = review.review_paper(review_data)
    except Exception as exc:
        print(f"  panel failed: {exc}", file=sys.stderr)
        _maybe_alert("ICSAC submission panel crashed",
                    f"{sub_id}: {type(exc).__name__}: {exc}")
        _a({"sub_id": sub_id, "event": "panel_crashed",
            "error": f"{type(exc).__name__}: {exc}"[:300]})
        return

    rec = aggregate.get("recommendation", "REVIEW_FURTHER")
    _log(f"  Recommendation: {rec}")

    # Persist the blind-review compaction manifest so apply_decision can
    # render the disclosure block in the author's decision email and the
    # audit trail records exactly what gemini stripped. review.review_paper
    # attaches it to aggregate unconditionally (including PAUSED_AI_FAILURE
    # paths) so this branch is the single place that touches disk.
    compaction_manifest = aggregate.get("compaction_manifest")
    if compaction_manifest is not None:
        (sub_dir / "compaction_manifest.json").write_text(
            json.dumps(compaction_manifest, indent=2) + "\n"
        )
        _a({
            "sub_id": sub_id,
            "event": "compaction_applied",
            "reduction_pct": compaction_manifest.get("reduction_pct"),
            "failure": compaction_manifest.get("_failure"),
            "removed_counts": {
                "authors": len(compaction_manifest.get("author_names", [])),
                "affiliations": len(compaction_manifest.get("affiliations", [])),
                "emails": len(compaction_manifest.get("emails", [])),
                "orcids": len(compaction_manifest.get("orcids", [])),
                "references": compaction_manifest.get("references_count", 0),
                "funding": len(compaction_manifest.get("funding_statements", [])),
            },
        })

    rqc_path = Path(config.REVIEWS_DIR) / f"{sub_id}_review_quality_control.md"
    rqc_flag = None
    if rqc_path.exists():
        match = re.search(
            r"review_quality_control_flag:\s*(true|false)",
            rqc_path.read_text(),
        )
        if match:
            rqc_flag = match.group(1) == "true"

    review_md_path = Path(config.REVIEWS_DIR) / f"{sub_id}_{_slug(review_data['title'])}.md"
    review_sha = None
    if review_md_path.exists():
        review_sha = hashlib.sha256(review_md_path.read_bytes()).hexdigest()

    # T2/T3: relocate the panel + RQC markdown into reviews/test/ so
    # production observability never sees these artifacts. We move
    # rather than write-twice — review.py wrote them under the prod
    # path; we shift them post-write so the panel module stays
    # tier-agnostic.
    if test_mode:
        TEST_REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        if review_md_path.exists():
            target = TEST_REVIEWS_DIR / review_md_path.name
            review_md_path.replace(target)
            review_md_path = target
        if rqc_path.exists():
            target = TEST_REVIEWS_DIR / rqc_path.name
            rqc_path.replace(target)
            rqc_path = target

    _a({
        "sub_id": sub_id, "event": "review_completed",
        "recommendation": rec, "rqc_flag": rqc_flag,
        "disagreement": aggregate.get("disagreement", False),
        "review_md_sha256": review_sha,
    })

    if rec == "PAUSED_AI_FAILURE":
        _write_state(sub_dir, state="paused_panel_failure",
                     panel_paused_at=_now_iso())
        # notify.alert_panel_failure already fired inside review_paper
        return

    # Every other recommendation routes to the curator. The panel
    # produces a recommendation, not a verdict; the curator confirms
    # or overrides before any author-facing action fires (publications
    # registration, Zenodo deposit staging, author email). apply_decision.py
    # runs those steps once the curator replies on the configured channel.
    _write_state(sub_dir, state="awaiting_decision",
                 panel_completed_at=_now_iso(),
                 pending_recommendation=rec)

    if test_mode:
        # T3 with TELEGRAM_TEST_CHAT_ID configured: fire the curator
        # escalation to the test chat so the full T3 spec
        # (worker-side Telegram + apply_decision-side email draft +
        # sandbox Zenodo) can be exercised end-to-end. T2 and
        # T3-without-test-chat skip the escalation entirely.
        if tier == 3 and getattr(config, "TELEGRAM_TEST_CHAT_ID", ""):
            test_chat = config.TELEGRAM_TEST_CHAT_ID
            test_thread = getattr(config, "TELEGRAM_TEST_THREAD_ID", "")
            _escalate_for_decision(sub_id, sub_dir, review_data, aggregate,
                                   chat_override=test_chat,
                                   thread_override=test_thread)
            notify.send_to_curator(
                f"{sub_id}: {rec} — awaiting curator verdict (T3 TEST)",
                parse_mode=None,
                chat_override=test_chat,
                thread_override=test_thread,
                ntfy=False,
            )
            _log(f"  T3: {rec} — escalated to test chat {test_chat}")
            _a({"sub_id": sub_id, "event": "awaiting_decision_test_escalated",
                "tier": 3, "recommendation": rec})
            return
        if tier == 3:
            _log(f"  T3: {rec} — TELEGRAM_TEST_CHAT_ID unset, escalation skipped")
        else:
            _log(f"  T{tier}: {rec} — escalation skipped on test path")
        _a({"sub_id": sub_id, "event": "awaiting_decision_skipped_test_mode",
            "recommendation": rec})
        return

    _escalate_for_decision(sub_id, sub_dir, review_data, aggregate)
    notify.send_to_curator(
        f"{sub_id}: {rec} — awaiting curator verdict",
        parse_mode=None,
    )


def drain() -> None:
    """Process every marker currently in queue/ and test/queue/, oldest
    first. Production markers and test-tier (T2/T3) markers are drained
    in the same pass since they run on the same worker; the per-marker
    process() call resolves tier from on-disk submission.json so the
    side-effect routing is decided per-submission, not per-queue.
    """
    queue_roots = [QUEUE_DIR, TEST_QUEUE_DIR]
    markers = []
    for q in queue_roots:
        if q.is_dir():
            markers.extend(q.iterdir())
    markers.sort(key=lambda p: p.stat().st_mtime)
    for marker in markers:
        sub_id = marker.name
        try:
            process(sub_id)
        except Exception as exc:
            _log(f"  worker error on {sub_id}: {exc}", err=True)
            # Alert only on the production path. Test-tier crashes log
            # to journald (already done by _log) but do not fire alerts.
            _, m_tier = _resolve_sub_dir(sub_id)
            if m_tier == 1:
                _alert_remote("ICSAC worker crash",
                           f"{sub_id}: {type(exc).__name__}: {exc}")
        finally:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # `python submission_worker.py ICSAC-SUB-00001` — process a single
        # submission directly (for backfill / manual rerun).
        process(sys.argv[1])
    else:
        drain()
