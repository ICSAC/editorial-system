#!/usr/bin/env python3
"""ICSAC Editorial System — workflow entry point."""

import argparse
import sys

import config
import submission_intake
import review
import notify
import action
import email_render
import email_send


def fire_pain(title, message):
    """Send pain signal to the operator's monitoring endpoint. Best-effort, never raises."""
    url = getattr(config, "NTFY_PAIN_URL", "")
    if not url:
        return
    import urllib.request
    try:
        req = urllib.request.Request(url, data=message.encode())
        req.add_header("Title", title)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def fire_brain(domain, sigtype, source, metric, value=1):
    """Push a brain signal. Best-effort, never raises."""
    url = getattr(config, "BRAIN_URL", "")
    if not url:
        return
    import urllib.request, json
    try:
        title = f"{domain}|{sigtype}|{source}|{metric}"
        req = urllib.request.Request(
            url,
            data=json.dumps({"value": value}).encode(),
        )
        req.add_header("Title", title)
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def fire_heartbeat(status="up", msg="OK"):
    """Push heartbeat to Uptime Kuma. Best-effort, never raises.

    Call from successful poll runs only — confirms the scheduled service ran
    end to end. Manual review invocations should NOT fire this (they'd create
    false 'all healthy' signals between scheduled polls).
    """
    base = getattr(config, "KUMA_PUSH_URL", "")
    if not base:
        return
    import urllib.request, urllib.parse
    try:
        params = urllib.parse.urlencode({"status": status, "msg": msg, "ping": ""})
        urllib.request.urlopen(f"{base}?{params}", timeout=5)
    except Exception:
        pass


def check_model_availability(timeout: int = 15) -> dict:
    """Fetch OR's live free-tier catalog and report per-configured-slot reachability.

    Returns a structured dict so both the CLI (refresh-models) and the batch
    tick scheduler can share one implementation. A slot is 'dead' when
    EVERY entry in its fallback chain is unreachable.

    Chain entries are backend-tagged (matching review._run_panel_chain):
      "or|<model>" / bare  → OpenRouter; validated against the live :free
                             catalog (the only backend this catalog covers).
      "hf|<model>:<prov>"  → HF Router (Groq/Cerebras) — not in OR's catalog,
                             so it can't be disproven here; treated reachable.
    (The "gemini" gemini-cli entry was retired 2026-05-22 ahead of the
    gemini-cli sunset; no slot ships a bare "gemini" anymore.)
    The pre-2026-05-16 version compared raw prefixed strings against the
    unprefixed OR catalog, so every tagged entry mismatched and all slots
    read 'dead' — falsely skipping reviews on every tick.

    Errors fetching the catalog surface as fetched=False; batch-tick treats
    this the same as a dead slot (can't confirm reachability → skip reviews).
    """
    import urllib.request, json as _json

    url = getattr(config, "OPENROUTER_MODELS_API_URL",
                  "https://openrouter.ai/api/v1/models")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode())
    except Exception as e:
        return {
            "fetched": False,
            "error": str(e),
            "free_models": [],
            "slots": [],
            "any_slot_dead": True,
        }

    free = [m for m in data.get("data", []) if m.get("id", "").endswith(":free")]
    free_ids = {m["id"] for m in free}
    free.sort(key=lambda m: -m.get("context_length", 0))

    def _entry_reachable(entry):
        # Mirror review._run_panel_chain's parsing: bare entries are OR.
        # (The "gemini" gemini-cli special case was retired 2026-05-22; a
        # stray "gemini" entry now parses as an OR model id and reads as
        # unreachable, matching the panel chain's post-retirement behavior.)
        kind, sep, model = entry.partition("|")
        if not sep:
            kind, model = "or", entry
        if kind == "or":
            return model in free_ids
        # hf| (Groq/Cerebras via HF Router) — not verifiable from OR catalog.
        return True

    slots_info = []
    for i, slot in enumerate(getattr(config, "OPENROUTER_MODELS", []), 1):
        chain = list(slot) if isinstance(slot, list) else [slot]
        reachable = [m for m in chain if _entry_reachable(m)]
        missing = [m for m in chain if not _entry_reachable(m)]
        slots_info.append({
            "index": i,
            "chain": chain,
            "reachable": reachable,
            "missing": missing,
            "dead": len(reachable) == 0,
        })

    return {
        "fetched": True,
        "free_models": free,
        "slots": slots_info,
        "any_slot_dead": any(s["dead"] for s in slots_info),
    }


def review_doi(doi: str, skip_notify: bool = False) -> dict:
    """Run the full review pipeline for a single DOI."""
    print(f"\n{'='*60}")
    print(f"Processing: {doi}")
    print(f"{'='*60}")

    # Ingest
    print("\n[1/3] Ingesting from Zenodo...")
    try:
        review_data = submission_intake.ingest_doi(doi)
    except Exception as e:
        print(f"  FAILED: {e}")
        return {"doi": doi, "error": str(e)}

    print(f"  Title: {review_data['title']}")
    print(f"  Authors: {', '.join(review._creator_display_names(review_data.get('creators')))}")
    pdf_status = "downloaded" if review_data.get("pdf_path") else "not available"
    print(f"  PDF: {pdf_status}")

    full_text_len = len(review_data.get("full_text", ""))
    if review_data.get("pdf_path") and full_text_len < submission_intake.PDF_TEXT_MIN_CHARS:
        msg = (
            f"PDF has no usable text layer ({full_text_len} chars extracted). "
            f"ICSAC requires text-layer PDFs — image-only scans and "
            f"raster-print deposits are not reviewed. Submitter must upload "
            f"a text-layer version."
        )
        print(f"  FAILED: {msg}")
        fire_pain(
            "ICSAC Pipeline: PDF requires text layer",
            f"{doi}: {review_data.get('title', '')[:120]}\n{msg}",
        )
        return {"doi": doi, "error": msg}

    # Review
    print("\n[2/3] Running reviewer panel...")
    try:
        markdown, aggregate = review.review_paper(review_data)
    except Exception as e:
        print(f"  FAILED: {e}")
        return {"doi": doi, "error": str(e)}

    rec = aggregate.get("recommendation", "REVIEW_FURTHER")
    print(f"  Recommendation: {rec}")
    print(f"  Disagreement: {aggregate.get('disagreement', False)}")

    # Notify
    if not skip_notify:
        print("\n[3/3] Sending notifications...")
        try:
            notify.notify_review_complete(review_data, aggregate)
            print("  Notifications sent.")
        except Exception as e:
            print(f"  Notification error (non-fatal): {e}")
    else:
        print("\n[3/3] Notifications skipped.")

    fire_brain("business", "reward", "editorial_system", "review_completed", 1)
    if rec == "RECOMMEND":
        fire_brain("business", "event", "editorial_system", "recommend", 1)
    elif rec == "REVISE_AND_RESUBMIT":
        fire_brain("business", "event", "editorial_system", "revise_and_resubmit", 1)
    elif rec == "REJECT":
        fire_brain("business", "event", "editorial_system", "reject", 1)

    return {
        "doi": doi,
        "title": review_data["title"],
        "recommendation": rec,
        "disagreement": aggregate.get("disagreement", False),
    }


def poll_community() -> None:
    """Poll for pending community requests and review them."""
    print("\nPolling ICSAC community requests...")
    requests = action.get_community_requests()

    if not requests:
        print("  No pending requests.")
        fire_heartbeat("up", "poll ok, 0 pending")
        return

    print(f"  Found {len(requests)} request(s).")
    fire_brain("business", "state", "editorial_system", "pending_requests", len(requests))
    for req in requests:
        topic = req.get("topic", {})
        record = topic.get("record", "")
        status = req.get("status", "")
        print(f"  - Request {req.get('id')}: record={record} status={status}")

    fire_heartbeat("up", f"poll ok, {len(requests)} pending")


def main():
    parser = argparse.ArgumentParser(
        description="ICSAC Open Review Pipeline"
    )
    sub = parser.add_subparsers(dest="command")

    # review command
    rev = sub.add_parser("review", help="Review one or more DOIs")
    rev.add_argument("dois", nargs="+", help="DOI(s) to review")
    rev.add_argument("--skip-notify", action="store_true", help="Skip notifications")

    # poll command
    sub.add_parser("poll", help="Poll community for pending requests")

    # requests command
    sub.add_parser("requests", help="List pending community requests")

    # accept/reject commands
    acc = sub.add_parser("accept", help="Accept a community request")
    acc.add_argument("request_id", help="Request ID to accept")
    acc.add_argument("--comment", default="", help="Comment for acceptance")

    rej = sub.add_parser("reject", help="Reject a community request")
    rej.add_argument("request_id", help="Request ID to reject")
    rej.add_argument("--comment", default="", help="Comment for rejection")

    sub.add_parser("watch-tick", help="Run one watcher cycle: detect transitions, fire side effects")
    sub.add_parser("watch-bootstrap", help="Seed state from current Zenodo state without firing side effects (run once on install)")

    refresh = sub.add_parser("refresh-models", help="Print currently-working free models from OpenRouter live API")
    refresh.add_argument("--check-exit", action="store_true",
                         help="Exit 2 if any configured slot has no reachable model (for cron health checks)")

    sub.add_parser("batch-tick", help="Run the twice-daily batch workflow: model check + watch-tick + summary")

    em = sub.add_parser("email", help="Render and (optionally) send accept / revise-and-resubmit / reject / invite emails")
    em.add_argument("kind", choices=["accept", "revise-and-resubmit", "reject", "invite"],
                    help=("accept = sends accept + community invite; "
                          "revise-and-resubmit = default decline path (engageable in-scope work); "
                          "reject = woo template (scope-not-suitable / pseudoscience only — NOT the standard decline path); "
                          "invite = resend invite only"))
    em.add_argument("doi", help="Zenodo DOI of the paper")
    em.add_argument("to", help="Recipient email address")
    em.add_argument("--send", action="store_true", help="Actually send (default: dry-run preview)")

    args = parser.parse_args()

    if args.command == "review":
        results = []
        for doi in args.dois:
            result = review_doi(doi, skip_notify=args.skip_notify)
            results.append(result)

        print(f"\n{'='*60}")
        print("BATCH SUMMARY")
        print(f"{'='*60}")
        for r in results:
            status = r.get("recommendation", r.get("error", "UNKNOWN"))
            print(f"  {r['doi']}: {status}")

    elif args.command == "poll":
        poll_community()

    elif args.command == "requests":
        requests = action.get_community_requests()
        for r in requests:
            print(f"  ID: {r.get('id')}  Status: {r.get('status')}")

    elif args.command == "accept":
        ok = action.accept_request(args.request_id, args.comment)
        print("Accepted." if ok else "Failed.")

    elif args.command == "reject":
        ok = action.reject_request(args.request_id, args.comment)
        print("Rejected." if ok else "Failed.")

    elif args.command == "refresh-models":
        result = check_model_availability()
        if not result["fetched"]:
            print(f"Failed to fetch models: {result.get('error', 'unknown')}")
            sys.exit(1)
        free = result["free_models"]
        print(f"\n=== {len(free)} FREE MODELS (live from OpenRouter) ===\n")
        print(f"{'MODEL':<60s} {'CTX':>10s}")
        print("-" * 75)
        for m in free:
            print(f"{m['id']:<60s} {m.get('context_length', 0):>10d}")
        print(f"\nCurrently configured slots:")
        for slot in result["slots"]:
            print(f"  Slot {slot['index']}: {' -> '.join(slot['chain'])}")
            for m in slot["chain"]:
                marker = "OK" if m in slot["reachable"] else "MISSING from free list"
                print(f"           {m}: {marker}")
            if slot["dead"]:
                print(f"           !! SLOT {slot['index']} IS DEAD (every fallback missing)")
        if args.check_exit and result["any_slot_dead"]:
            sys.exit(2)
        sys.exit(0)

    elif args.command == "batch-tick":
        import watch
        import notify
        import publish_watcher

        print("== ICSAC Batch Tick ==")
        print("[1/4] Checking OR model availability...")
        mod = check_model_availability()
        skip_reviews = False
        if not mod["fetched"]:
            print(f"  catalog fetch failed: {mod.get('error')}")
            skip_reviews = True
        else:
            dead = [s for s in mod["slots"] if s["dead"]]
            if dead:
                for s in dead:
                    print(f"  SLOT {s['index']} DEAD — chain {s['chain']} all missing")
                skip_reviews = True
            else:
                print(f"  all {len(mod['slots'])} OR slots have >=1 reachable model")

        if skip_reviews:
            dead_slots = [s["index"] for s in mod.get("slots", []) if s["dead"]]
            fire_pain(
                "ICSAC Batch Tick: review step skipped",
                (
                    f"OR model availability check failed (fetched={mod['fetched']}, "
                    f"dead_slots={dead_slots}). State transitions still handled; "
                    f"new submissions tracked but not reviewed until next healthy tick."
                ),
            )

        print(f"[2/4] Running watch tick (skip_reviews={skip_reviews})...")
        import sys as _sys
        _sys.argv = ["watch"] + (["--skip-reviews"] if skip_reviews else [])
        rc = watch.main()

        print("[3/4] Polling staged Zenodo drafts for publish transitions...")
        try:
            publish_summary = publish_watcher.poll_drafts()
            print(
                f"  publish_watcher: checked={publish_summary['checked']} "
                f"published={publish_summary['published']} "
                f"still_draft={publish_summary['still_draft']} "
                f"errors={publish_summary['errors']}"
            )
        except Exception as e:
            print(f"  publish_watcher crashed (non-fatal): {e}")
            publish_summary = {"checked": 0, "published": 0,
                               "still_draft": 0, "errors": 1,
                               "transitions": []}

        print("[4/4] Summary curator alert...")
        try:
            dead_slots = [s["index"] for s in mod.get("slots", []) if s["dead"]]
            if mod["fetched"]:
                model_status = (
                    f"{len(mod['slots']) - len(dead_slots)}/{len(mod['slots'])} OR slots live"
                    + (f" (dead: {dead_slots})" if dead_slots else "")
                )
            else:
                model_status = f"catalog fetch failed: {mod.get('error', 'unknown')[:80]}"
            publish_line = (
                f"Publish-watcher: {publish_summary['published']} new, "
                f"{publish_summary['still_draft']} still draft, "
                f"{publish_summary['errors']} errors"
            )
            if publish_summary["transitions"]:
                publish_line += f" — {', '.join(publish_summary['transitions'])}"
            msg = (
                f"ICSAC Batch Tick complete\n\n"
                f"Models: {model_status}\n"
                f"Reviews: {'SKIPPED (starved panel)' if skip_reviews else 'ran'}\n"
                f"Watch tick exit: {rc}\n"
                f"{publish_line}\n\n"
                f"Transitions (accept/decline) always run regardless of model state."
            )
            notify.send_to_curator(msg, parse_mode=None)
        except Exception as e:
            print(f"  summary curator alert failed (non-fatal): {e}")

        sys.exit(rc)

    elif args.command == "email":
        import time
        review_data = submission_intake.ingest_doi(args.doi)

        def _deliver(label: str, rendered: str, send_fn) -> bool:
            print(f"\n=== {label} ===")
            print(rendered)
            ok, msg = send_fn(args.to, rendered, send=args.send)
            print("=== DELIVERY ===")
            print(("OK" if ok else "FAIL") + ": " + msg)
            return ok

        if args.kind == "accept":
            ok1 = _deliver("EMAIL 1/2 — ACCEPT",
                           email_render.render_accept_email(review_data),
                           email_send.send_accept_email)
            if not ok1:
                if not args.send:
                    print("\n(dry-run; pass --send to actually deliver)")
                sys.exit(1)
            if args.send:
                time.sleep(5)
            ok2 = _deliver("EMAIL 2/2 — COMMUNITY INVITE",
                           email_render.render_community_invite_email(review_data),
                           email_send.send_invite_email)
            if not args.send:
                print("\n(dry-run; pass --send to actually deliver both)")
            sys.exit(0 if (ok1 and ok2) else 1)
        elif args.kind == "revise-and-resubmit":
            ok = _deliver("REVISE-AND-RESUBMIT EMAIL",
                          email_render.render_revise_and_resubmit_email(review_data),
                          email_send.send_revise_and_resubmit_email)
        elif args.kind == "reject":
            ok = _deliver("SCOPE-REJECT EMAIL (scope-not-suitable)",
                          email_render.render_scope_reject_email(review_data),
                          email_send.send_scope_reject_email)
        else:  # invite
            ok = _deliver("COMMUNITY INVITE",
                          email_render.render_community_invite_email(review_data),
                          email_send.send_invite_email)
        if not args.send:
            print("\n(dry-run; pass --send to actually deliver)")
        sys.exit(0 if ok else 1)

    elif args.command == "watch-tick":
        import watch
        sys.exit(watch.main())

    elif args.command == "watch-bootstrap":
        import watch
        sys.argv = ["watch", "--bootstrap"]
        sys.exit(watch.main())

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        fire_pain(
            "editorial-system failed",
            f"Pipeline crashed: {type(exc).__name__}: {exc}",
        )
        raise
