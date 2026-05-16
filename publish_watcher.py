"""Poll staged-draft Zenodo deposits for publish-status transitions.

PDF-route ICSAC submissions stage a draft Zenodo deposit on accept but
NEVER auto-publish (per feedback_zenodo_drafts_only.md — minted DOIs are
permanent, the operator publishes manually). This module provides the
inverse poll: scan the on-disk submission state, hit Zenodo per-draft to
check whether the operator has published, and on transition:

  1. Update the submission state.json with deposit_doi + deposit_url.
  2. Upsert a publications-registry entry with the now-real DOI.
  3. Stage the redacted panel review under public-reviews/<slug>.{md,html}
     and commit + push to the website repo.
  4. Send a post-publish notification email to the author with the DOI
     and the canonical icsacinstitute.org/publications/<slug> permalink.

Invoked from editorial_workflow.py batch-tick. Cost is bounded — one Zenodo HTTP
hit per draft in `awaiting_publish` state. Failures on individual drafts
fire /pain but never block other drafts in the same poll.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import config
import publications


SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
# Intake-side author-notify helper is imported lazily inside _emit_published()
# below — keeps the watcher decoupled from FastAPI / WeasyPrint at module-
# import time.


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _fire_pain(title: str, body: str) -> None:
    """Direct ntfy /pain POST to the monitoring endpoint. Best-effort, never raises."""
    url = getattr(config, "NTFY_PAIN_URL", "")
    if not url:
        return
    try:
        req = urllib.request.Request(url, data=body.encode())
        req.add_header("Title", title)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _get_deposit(record_id: str) -> dict:
    """Fetch deposit status for an ICSAC-owned record.

    Uses /api/deposit/depositions/<id> (token-required) which works for
    both drafts and published records. The published-record endpoint
    /api/records/<id> 404s on drafts, so the deposit endpoint is the
    single uniform check.
    """
    url = f"{config.ZENODO_API}/deposit/depositions/{record_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _list_awaiting_publish() -> list[Path]:
    """Find submission dirs with a staged draft but no minted DOI yet."""
    if not SUBMISSIONS_ROOT.is_dir():
        return []
    out: list[Path] = []
    for sub_dir in SUBMISSIONS_ROOT.iterdir():
        if not sub_dir.is_dir() or sub_dir.name == "queue":
            continue
        state_path = sub_dir / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            continue
        if state.get("deposit_record_id") and not state.get("deposit_doi"):
            out.append(sub_dir)
    return out


def _bare_doi(s: str) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s or "").strip()


def _proto_authors_from_submission(submission: dict) -> list[str]:
    """Mirror intake.submission_worker._proto_authors — kept here to avoid
    pulling in the intake subpackage for a five-line helper."""
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


def _register_published(sub_dir: Path, submission: dict, deposit: dict) -> dict:
    """Build the proto, upsert publications, stage redacted review, push.

    Returns the canonical entry (with .slug populated). Caller updates
    state.json + sends author email after this returns.
    """
    sub_id = sub_dir.name
    metadata = deposit.get("metadata") or {}
    title = (
        metadata.get("title")
        or submission.get("title")
        or "Untitled"
    )
    abstract = submission.get("abstract") or ""
    doi = _bare_doi(deposit.get("doi") or metadata.get("doi") or "")
    if not doi:
        raise ValueError(
            f"published deposit for {sub_id} has no DOI in response"
        )

    record_id = str(deposit.get("record_id") or deposit.get("id") or "")

    proto = {
        "title": title,
        "authors": _proto_authors_from_submission(submission),
        "doi": doi,
        "abstract": abstract[:2000],
        "source": "submission-pdf",
        "source_ref": f"https://zenodo.org/records/{record_id}" if record_id else f"https://doi.org/{doi}",
    }
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
    return entry


def _notify_author_published(submission: dict, sub_id: str,
                             entry: dict, deposit_doi: str,
                             deposit_url: str) -> bool:
    """Send the short post-publish email. Best-effort; logs and returns False on failure."""
    from intake import notify_author as intake_notify  # noqa: WPS433
    form = submission.get("form") or {}
    to = form.get("email")
    if not to:
        print(f"  publish_watcher: {sub_id} has no form.email; skipping author notify",
              file=sys.stderr)
        return False
    try:
        ok, info = intake_notify.send_published(
            to=to, sub_id=sub_id,
            title=entry["title"],
            author_name=form.get("name") or "Author",
            deposit_doi=deposit_doi,
            deposit_url=deposit_url,
            publications_url=publications.publications_url(entry["slug"]),
        )
        if not ok:
            print(f"  publish_watcher: send_published failed for {sub_id}: {info}",
                  file=sys.stderr)
        return bool(ok)
    except Exception as exc:
        print(f"  publish_watcher: send_published crashed for {sub_id}: {exc}",
              file=sys.stderr)
        return False


def poll_drafts() -> dict:
    """Walk every submission with a staged draft, register on transition.

    Returns a small summary dict suitable for inclusion in the batch-tick
    curator digest: {checked, published, skipped, errors}.
    """
    drafts = _list_awaiting_publish()
    summary = {
        "checked": len(drafts),
        "published": 0,
        "still_draft": 0,
        "errors": 0,
        "transitions": [],  # list of sub_ids that flipped
    }
    for sub_dir in drafts:
        sub_id = sub_dir.name
        state_path = sub_dir / "state.json"
        sub_path = sub_dir / "submission.json"
        try:
            state = json.loads(state_path.read_text())
            submission = json.loads(sub_path.read_text())
        except Exception as exc:
            print(f"  publish_watcher: {sub_id} unreadable: {exc}",
                  file=sys.stderr)
            summary["errors"] += 1
            continue

        record_id = state.get("deposit_record_id")
        try:
            deposit = _get_deposit(record_id)
        except urllib.error.HTTPError as e:
            print(f"  publish_watcher: {sub_id} deposit fetch HTTP {e.code}",
                  file=sys.stderr)
            summary["errors"] += 1
            continue
        except Exception as exc:
            print(f"  publish_watcher: {sub_id} deposit fetch failed: {exc}",
                  file=sys.stderr)
            summary["errors"] += 1
            continue

        is_published = bool(
            deposit.get("submitted")
            and deposit.get("state") == "done"
            and (deposit.get("doi") or (deposit.get("metadata") or {}).get("doi"))
        )
        if not is_published:
            summary["still_draft"] += 1
            continue

        deposit_doi = _bare_doi(deposit.get("doi") or (deposit.get("metadata") or {}).get("doi") or "")
        deposit_url = (
            (deposit.get("links") or {}).get("record_html")
            or f"https://zenodo.org/records/{record_id}"
        )

        try:
            entry = _register_published(sub_dir, submission, deposit)
        except Exception as exc:
            print(f"  publish_watcher: register failed for {sub_id}: {exc}",
                  file=sys.stderr)
            _fire_pain(
                "ICSAC publish_watcher: register failed",
                f"{sub_id} (record {record_id}) is published on Zenodo with "
                f"DOI {deposit_doi or 'unknown'} but the publications "
                f"registry write failed: {type(exc).__name__}: {exc}. "
                f"State.json NOT updated; the next batch tick will retry.",
            )
            summary["errors"] += 1
            continue

        # State write only after the registry commit succeeded so a crash
        # mid-flow doesn't leave the system thinking it's done.
        state["deposit_doi"] = deposit_doi
        state["deposit_url"] = deposit_url
        state["state"] = "published"
        state["published_at"] = _now_iso()
        state_path.write_text(json.dumps(state, indent=2))

        _notify_author_published(submission, sub_id, entry,
                                 deposit_doi, deposit_url)

        print(
            f"  publish_watcher: {sub_id} → published "
            f"doi={deposit_doi} slug={entry['slug']}"
        )
        summary["published"] += 1
        summary["transitions"].append(sub_id)
    return summary


if __name__ == "__main__":
    s = poll_drafts()
    print(json.dumps(s, indent=2))
