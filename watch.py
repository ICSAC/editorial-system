"""Watcher for ICSAC community-inclusion requests.

Polls /api/user/requests, diffs against state/watched.json, fires side effects
on transitions:

  unknown → submitted (open):    run review (panel + write markdown locally)
  submitted/reviewed → accepted: post branded comment + register landing page
  submitted/reviewed → declined: post branded decline comment with review summary
  submitted/reviewed → cancelled: no action (author withdrew)

Fully automated. The only human step is the click in the Zenodo curator UI.
The branded comment is delivered to the author by Zenodo's notification machinery,
so we do not need to discover author emails.

State file format (state/watched.json):
{
  "<request_id>": {
    "record_id": "...",
    "title": "...",
    "first_seen": "iso",
    "status": "submitted|reviewed|accepted|declined|cancelled",
    "review_path": "reviews/<id>_<slug>.md" or null,
    "last_check": "iso"
  }
}

Bootstrap mode marks every currently-visible request with its current status
WITHOUT firing side effects, so we don't re-fire emails for historical state.

Pain wiring: any uncaught exception in tick() fires /pain. Successful tick
also pings the Uptime Kuma push monitor for silence detection.
"""

import datetime
import json
import os
import sys
import traceback
import urllib.error
import urllib.request

import action
import config
import email_render
import ingest
import notify
import scrubber


STATE_DIR = os.path.join(config.BASE_DIR, "state")
STATE_PATH = os.path.join(STATE_DIR, "watched.json")
PAIN_URL = "http://100.117.63.73:8090/pain"
KUMA_PUSH_URL = "http://100.117.63.73:3001/api/push/bOaUZKHaJC"

TERMINAL_STATUSES = {"accepted", "declined", "cancelled", "expired"}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_state() -> dict:
    if not os.path.isfile(STATE_PATH):
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def _fire_pain(title: str, body: str) -> None:
    try:
        req = urllib.request.Request(PAIN_URL, data=body.encode())
        req.add_header("Title", title)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _ping_kuma(status: str = "up", msg: str = "") -> None:
    try:
        url = f"{KUMA_PUSH_URL}?status={status}&msg={urllib.request.quote(msg)}"
        urllib.request.urlopen(url, timeout=5)
    except Exception:
        pass


def _safe_post_comment(request_id: str, body: str, kind: str, context: str) -> bool:
    """Run the scrubber grep-gate on a rendered Zenodo-comment body before posting.

    The accept/decline comment includes text pulled from the on-disk review
    (summary, concerns). A poisoned review that survived the panel's own
    defenses could still leak through this pass-through path — this gate
    catches credential prefixes, filesystem paths, env-var assignments, and
    vendor/model tokens before the body reaches Zenodo.

    On a fatal hit the comment is NOT posted. Zenodo has already delivered
    its own state-change notification to the author, so the author still
    learns the decision; only our branded follow-up is suppressed. /pain
    fires so the operator can inspect and post a cleaned comment manually.
    """
    try:
        scrubber.assert_clean(body, artifact_path=f"{kind}-comment:{request_id}")
    except scrubber.ScrubLeak as e:
        print(f"  {kind} comment blocked by scrub gate: {e}")
        _fire_pain(
            f"ICSAC Watcher: {kind} comment blocked by scrub gate",
            (
                f"{e}\n\nContext: {context}\n"
                f"The branded {kind} comment was NOT posted to request {request_id}. "
                f"Zenodo's own state-change notification still reached the author. "
                f"Inspect the rendered comment, redact the leak, and post manually "
                f"via `python3 -c 'import action; action.post_request_comment(...)'`."
            ),
        )
        return False
    return action.post_request_comment(request_id, body, fmt="html")


def _parse_review_flags(review_path: str | None) -> tuple[bool, bool]:
    """Read the review + RQC markdown frontmatter to extract gate flags.

    Returns (disagreement, rqc_flag). Either true means the auto-posted
    Zenodo comment must be suppressed and the operator must approve the
    branded follow-up manually.

    Missing files are treated as (False, False) — absence of signal, not
    presence of agreement. The operator still sees Zenodo's own decision
    notification; only our branded follow-up is gated.
    """
    disagreement = False
    rqc_flag = False
    if review_path and os.path.isfile(review_path):
        try:
            with open(review_path) as f:
                text = f.read()
            fm = {}
            if text.startswith("---\n"):
                end = text.find("\n---\n", 4)
                if end > 0:
                    for line in text[4:end].splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            fm[k.strip()] = v.strip().strip('"').strip("'")
            disagreement = fm.get("disagreement", "False").lower() == "true"
        except Exception:
            pass
    if review_path:
        record_id = os.path.basename(review_path).split("_", 1)[0]
        rqc_path = os.path.join(os.path.dirname(review_path), f"{record_id}_review_quality_control.md")
        if os.path.isfile(rqc_path):
            try:
                with open(rqc_path) as f:
                    text = f.read()
                if text.startswith("---\n"):
                    end = text.find("\n---\n", 4)
                    if end > 0:
                        for line in text[4:end].splitlines():
                            if line.strip().startswith("review_quality_control_flag:"):
                                val = line.split(":", 1)[1].strip().strip('"').strip("'")
                                rqc_flag = val.lower() == "true"
                                break
            except Exception:
                pass
    return disagreement, rqc_flag


def _escalate_comment(rid: str, record_id: str, title: str, kind: str,
                      comment_md: str, disagreement: bool, rqc_flag: bool) -> None:
    """Suppress auto-posting the branded Zenodo comment; notify the operator.

    The watcher calls this when the on-disk review signals panel disagreement
    or a Review Quality Control flag. Zenodo still delivers its own state-change
    notification to the author, so the author still learns the decision; only
    the ICSAC-branded follow-up is held pending operator review.
    """
    reasons = []
    if disagreement:
        reasons.append("panel disagreement")
    if rqc_flag:
        reasons.append("RQC flagged")
    reason_str = " + ".join(reasons) or "quality gate"

    print(f"  {kind} comment gated ({reason_str}); escalating to operator")
    msg = (
        f"ICSAC Pipeline — {kind.capitalize()} Comment Held\n\n"
        f"Record: {record_id}\n"
        f"Title: {title[:160]}\n"
        f"Reason: {reason_str}\n\n"
        f"Zenodo's state-change notification reached the author. The ICSAC-branded "
        f"{kind} comment is held pending your review. Inspect the rendered comment "
        f"below, adjust if needed, then post manually via "
        f"`python3 -c 'import action; action.post_request_comment(\"{rid}\", BODY, fmt=\"html\")'`.\n\n"
        f"--- Rendered comment body ---\n{comment_md[:3500]}"
    )
    notify.send_telegram(msg, parse_mode=None)
    _fire_pain(
        f"ICSAC Watcher: {kind} comment held ({reason_str})",
        f"Record {record_id}: {title[:120]}\nReason: {reason_str}\nCheck Telegram for the rendered comment body.",
    )


def _fetch_record_metadata(record_id: str) -> dict:
    """Fetch a record's Zenodo metadata. Public endpoint — no auth needed."""
    url = f"{config.ZENODO_API}/records/{record_id}"
    req = urllib.request.Request(url)
    if config.ZENODO_TOKEN:
        req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _doi_from_record(record_id: str) -> str:
    md = _fetch_record_metadata(record_id)
    return md.get("doi") or md.get("metadata", {}).get("doi", "")


def _review_data_from_record(record_id: str, review_path: str | None) -> dict:
    """Build the dict that email_render._base_data expects.

    Pulls metadata via ingest.ingest_doi (which uses Zenodo's record API)
    and overlays the local review's recommendation/disagreement if available.
    """
    doi = _doi_from_record(record_id)
    data = ingest.ingest_doi(doi) if doi else {"record_id": record_id}
    data["record_id"] = str(record_id)
    return data


def _generate_review(record_id: str) -> str | None:
    """Run the review panel for a record. Returns the review markdown path,
    or None on failure."""
    import pipeline as pl
    doi = _doi_from_record(record_id)
    if not doi:
        print(f"  no DOI for record {record_id}; skipping review")
        return None
    print(f"  generating review for {doi}")
    try:
        result = pl.review_doi(doi, skip_notify=True)
    except Exception as e:
        print(f"  review failed: {e}")
        return None
    review_path = result.get("review_path") if isinstance(result, dict) else None
    if not review_path:
        # review_doi historically didn't return path — find it under reviews/
        candidates = [
            os.path.join(config.REVIEWS_DIR, f)
            for f in os.listdir(config.REVIEWS_DIR)
            if f.startswith(f"{record_id}_") and f.endswith(".md")
        ]
        review_path = max(candidates, key=os.path.getmtime) if candidates else None
    return review_path


def _handle_new_submission(req: dict, state: dict, skip_review: bool = False) -> None:
    """Generate a review for a newly-seen open submission.

    When skip_review=True, the submission is still tracked in state but
    review generation is deferred until the next tick with a healthy
    reviewer panel. Status stays 'submitted' so a later tick picks it up.
    """
    rid = req["id"]
    record_id = str(req["topic"]["record"])
    raw_title = req.get("title") or ""
    if isinstance(raw_title, dict):
        title = raw_title.get("title", "")
    else:
        title = str(raw_title)
    title = title or _record_title(record_id)
    print(f"NEW SUBMISSION: request={rid[:8]} record={record_id} — {title[:80]}")

    # Skip review if one already exists on disk (covers re-runs, bootstrap)
    existing = _find_existing_review(record_id)
    if existing:
        print(f"  review already on disk: {existing}")
        review_path = existing
    elif skip_review:
        print(f"  review deferred (skip_reviews=True); tracking submission only")
        review_path = None
    else:
        review_path = _generate_review(record_id)

    state[rid] = {
        "record_id": record_id,
        "title": title[:200],
        "first_seen": _now_iso(),
        "status": "reviewed" if review_path else "submitted",
        "review_path": review_path,
        "last_check": _now_iso(),
    }


def _handle_accept_transition(req: dict, state_entry: dict) -> None:
    """Curator accepted the request via UI/API. Post our comment + register paper."""
    rid = req["id"]
    record_id = state_entry["record_id"]
    title = state_entry.get("title", "")
    print(f"ACCEPT TRANSITION: request={rid[:8]} record={record_id} — {title[:80]}")

    # Comment first (lightweight, idempotency is on us — the watcher only fires
    # this branch once per request because we then mark state.status=accepted).
    # Quality gate: if the on-disk review shows panel disagreement or the RQC
    # audit tripped, the branded comment is held for operator review rather
    # than auto-posted. The landing-page registry still publishes so the
    # accept itself is not blocked.
    disagreement, rqc_flag = _parse_review_flags(state_entry.get("review_path"))
    try:
        review_data = _review_data_from_record(record_id, state_entry.get("review_path"))
        landing_url = f"https://icsacinstitute.org/accepted/{record_id}"
        comment_md = email_render.render_accept_comment(review_data, landing_url=landing_url)
        if disagreement or rqc_flag:
            _escalate_comment(rid, record_id, title, "accept", comment_md, disagreement, rqc_flag)
        else:
            ok = _safe_post_comment(rid, comment_md, "accept", context=title[:120])
            print(f"  branded comment posted: {ok}")
    except Exception as e:
        print(f"  comment post failed (non-fatal): {e}")
        _fire_pain(
            "ICSAC Watcher: accept comment failed",
            f"Could not post accept comment to request {rid} (record {record_id}): {e}",
        )

    # Then register on the website (landing page + scrubbed review + stats + push)
    try:
        action.register_accepted_paper(record_id)
    except Exception as e:
        print(f"  registry update failed: {e}")
        _fire_pain(
            "ICSAC Watcher: registry push failed",
            f"Accept comment posted but landing-page registry push failed for "
            f"record {record_id}: {e}",
        )


def _handle_decline_transition(req: dict, state_entry: dict) -> None:
    """Curator declined the request via UI/API. Post our decline comment."""
    rid = req["id"]
    record_id = state_entry["record_id"]
    title = state_entry.get("title", "")
    print(f"DECLINE TRANSITION: request={rid[:8]} record={record_id} — {title[:80]}")

    disagreement, rqc_flag = _parse_review_flags(state_entry.get("review_path"))
    try:
        review_data = _review_data_from_record(record_id, state_entry.get("review_path"))
        # Pull the recommendation summary from the on-disk review if present.
        summary, concerns = _extract_review_blurb(state_entry.get("review_path"))
        comment_md = email_render.render_decline_comment(
            review_data,
            review_summary=summary,
            specific_concerns=concerns,
        )
        if disagreement or rqc_flag:
            _escalate_comment(rid, record_id, title, "decline", comment_md, disagreement, rqc_flag)
        else:
            ok = _safe_post_comment(rid, comment_md, "decline", context=title[:120])
            print(f"  branded decline comment posted: {ok}")
    except Exception as e:
        print(f"  decline comment failed: {e}")
        _fire_pain(
            "ICSAC Watcher: decline comment failed",
            f"Could not post decline comment to request {rid} (record {record_id}): {e}",
        )


def _extract_review_blurb(review_path: str | None) -> tuple[str, str]:
    """Pull a short summary + concerns string from the review markdown.

    Used to fill the decline comment. Falls back to generic text if parsing fails.
    """
    if not review_path or not os.path.isfile(review_path):
        return ("", "")
    try:
        with open(review_path) as f:
            txt = f.read()
    except Exception:
        return ("", "")
    summary, concerns = "", ""
    # Pull the first "Summary" / "Concerns" sections if present.
    # Reviews vary in shape — best-effort.
    for hdr, target in (("## Summary", "summary"), ("## Concerns", "concerns"),
                        ("### Summary", "summary"), ("### Key Concerns", "concerns")):
        if hdr in txt:
            chunk = txt.split(hdr, 1)[1].split("\n##", 1)[0].strip()
            chunk = chunk[:600]
            if target == "summary":
                summary = chunk
            else:
                concerns = chunk
    return (summary, concerns)


def _find_existing_review(record_id: str) -> str | None:
    if not os.path.isdir(config.REVIEWS_DIR):
        return None
    candidates = [
        os.path.join(config.REVIEWS_DIR, f)
        for f in os.listdir(config.REVIEWS_DIR)
        if f.startswith(f"{record_id}_") and f.endswith(".md")
    ]
    return max(candidates, key=os.path.getmtime) if candidates else None


def _record_title(record_id: str) -> str:
    try:
        md = _fetch_record_metadata(record_id)
        return md.get("metadata", {}).get("title", "") or ""
    except Exception:
        return ""


def tick(bootstrap: bool = False, skip_reviews: bool = False) -> None:
    """One polling cycle. Fetches all ICSAC requests (open + closed) so we can
    detect transitions. Fires side effects only outside of bootstrap mode.

    skip_reviews=True defers review generation (used by batch-tick when the
    OR model availability check fails). Transitions always run — accept/decline
    comments + landing-page publication don't depend on reviewer panel health.
    """
    state = _load_state()
    requests = action.get_community_requests(open_only=False)
    print(f"watch-tick: {len(requests)} ICSAC requests visible "
          f"(bootstrap={bootstrap}, skip_reviews={skip_reviews})")
    fired = {"new": 0, "accept": 0, "decline": 0, "cancel": 0,
             "deferred_review": 0, "noop": 0}

    for req in requests:
        rid = req["id"]
        zstatus = req.get("status", "submitted")
        prior = state.get(rid)

        if prior is None:
            # First sighting
            if bootstrap:
                state[rid] = {
                    "record_id": str(req["topic"]["record"]),
                    "title": _record_title(str(req["topic"]["record"]))[:200],
                    "first_seen": _now_iso(),
                    "status": zstatus,
                    "review_path": _find_existing_review(str(req["topic"]["record"])),
                    "last_check": _now_iso(),
                }
                fired["noop"] += 1
                continue
            if zstatus == "submitted":
                _handle_new_submission(req, state, skip_review=skip_reviews)
                fired["new"] += 1
            else:
                # Closed before we ever saw it open — just record, do nothing.
                state[rid] = {
                    "record_id": str(req["topic"]["record"]),
                    "title": _record_title(str(req["topic"]["record"]))[:200],
                    "first_seen": _now_iso(),
                    "status": zstatus,
                    "review_path": _find_existing_review(str(req["topic"]["record"])),
                    "last_check": _now_iso(),
                }
                fired["noop"] += 1
            continue

        # Already in state — check for transitions.
        prior_status = prior.get("status")
        if prior_status in TERMINAL_STATUSES:
            prior["last_check"] = _now_iso()
            fired["noop"] += 1
            continue

        # Deferred-review recovery: a prior tick skipped the review because
        # the panel was starved. If we're healthy now AND the submission is
        # still open, try to generate the review this tick.
        if (prior_status == "submitted"
                and not prior.get("review_path")
                and not skip_reviews
                and zstatus == "submitted"):
            print(f"DEFERRED REVIEW: request={rid[:8]} record={prior['record_id']}")
            review_path = _generate_review(prior["record_id"])
            if review_path:
                prior["review_path"] = review_path
                prior["status"] = "reviewed"
            fired["deferred_review"] += 1

        if zstatus == prior_status:
            prior["last_check"] = _now_iso()
            fired["noop"] += 1
            continue

        # Transition!
        if zstatus == "accepted":
            if not bootstrap:
                _handle_accept_transition(req, prior)
                fired["accept"] += 1
            prior["status"] = "accepted"
        elif zstatus == "declined":
            if not bootstrap:
                _handle_decline_transition(req, prior)
                fired["decline"] += 1
            prior["status"] = "declined"
        elif zstatus == "cancelled":
            prior["status"] = "cancelled"
            fired["cancel"] += 1
        elif zstatus == "submitted":
            # Reopened? Just track; do not re-review.
            prior["status"] = "submitted"
            fired["noop"] += 1
        prior["last_check"] = _now_iso()

    _save_state(state)
    summary = ", ".join(f"{k}={v}" for k, v in fired.items())
    print(f"watch-tick done: {summary} (bootstrap={bootstrap})")
    _ping_kuma("up", f"watch-tick ok: {summary}")


def main() -> int:
    bootstrap = "--bootstrap" in sys.argv
    skip_reviews = "--skip-reviews" in sys.argv
    try:
        tick(bootstrap=bootstrap, skip_reviews=skip_reviews)
        return 0
    except Exception as e:
        traceback.print_exc()
        _fire_pain(
            "ICSAC Watcher: tick crash",
            f"watch.py tick failed: {e}\n\n{traceback.format_exc()[:1500]}",
        )
        _ping_kuma("down", f"watch-tick crash: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
