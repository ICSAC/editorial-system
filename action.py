"""Accept/reject Zenodo community requests via API.

Accept also writes the paper to the icsacinstitute.org website registry
(src/data/accepted.json) and commits+pushes the change, which triggers
CF Pages to rebuild. That rebuild publishes an ICSAC-branded landing page
at https://icsacinstitute.org/accepted/<record_id> so LinkedIn and Facebook
shares show ICSAC metadata rather than generic Zenodo cards.

The accept path also scrubs the internal review (reviews/<id>_*.md) and
writes a publication-safe copy to the website repo at
src/data/public-reviews/<record_id>.md, embedded on the landing page. The
scrubber's grep-gate aborts publication if any forbidden token survives —
a scrub leak fires /pain and leaves the Zenodo accept intact but the
registry + review unpushed.
"""

import datetime
import html
import json
import os
import re
import subprocess
import urllib.request
import urllib.error

import config
import publications
import scrubber
import stats as stats_mod


WEBSITE_REPO = publications.WEBSITE_REPO
REGISTRY_PATH = publications.REGISTRY_PATH
PUBLIC_REVIEWS_DIR = os.path.join(WEBSITE_REPO, "src/data/public-reviews")


_COMMUNITY_UUID_CACHE: str | None = None


def _resolve_community_uuid() -> str:
    """Look up the ICSAC community UUID from its slug. Cached for the process.

    /api/user/requests filters by community UUID, not slug. /api/communities/<slug>
    works as a lookup endpoint and does not require curator scope.
    """
    global _COMMUNITY_UUID_CACHE
    if _COMMUNITY_UUID_CACHE:
        return _COMMUNITY_UUID_CACHE
    url = f"{config.ZENODO_API}/communities/{config.COMMUNITY_ID}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    _COMMUNITY_UUID_CACHE = data["id"]
    return _COMMUNITY_UUID_CACHE


def get_community_requests(open_only: bool = True) -> list[dict]:
    """Fetch ICSAC community-inclusion requests via /api/user/requests.

    The historical /api/communities/<id>/requests endpoint requires a curator
    scope that personal access tokens cannot grant. /api/user/requests returns
    every request the authenticated user is involved in, including incoming
    community-inclusion requests for communities they own. We filter client-side
    to community-inclusion + ICSAC + (optionally) is_open.
    """
    icsac_uuid = _resolve_community_uuid()
    out: list[dict] = []
    page = 1
    while page <= 20:  # 20 pages * 100 items = hard ceiling
        url = f"{config.ZENODO_API}/user/requests?size=100&page={page}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            print(f"  Error fetching user requests page {page}: {e}")
            break
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break
        for r in hits:
            if r.get("type") != "community-inclusion":
                continue
            if (r.get("receiver") or {}).get("community") != icsac_uuid:
                continue
            if open_only and not r.get("is_open"):
                continue
            out.append(r)
        if len(hits) < 100:
            break
        page += 1
    return out


def accept_request(request_id: str, comment: str = "",
                   review_data: dict | None = None) -> bool:
    """Accept a community inclusion request.

    If review_data is supplied, an ICSAC-branded acceptance comment is rendered
    and posted with the action — Zenodo notifies the author via its own email
    machinery. The comment points to the public landing page on icsacinstitute.org.

    Registry update (landing page + scrubbed review + stats) runs after accept
    succeeds. Registry failure does NOT fail the Zenodo accept — it is logged
    and skipped (a /pain alert fires).
    """
    if review_data and not comment:
        import email_render
        record_id_hint = review_data.get("record_id") or _get_request_record_id(request_id)
        landing_url = (
            f"https://icsacinstitute.org/accepted/{record_id_hint}"
            if record_id_hint else "https://icsacinstitute.org"
        )
        comment = email_render.render_accept_comment(review_data, landing_url=landing_url)
    ok = _action_request(request_id, "accept", comment)
    if ok:
        try:
            record_id = _get_request_record_id(request_id)
            if record_id:
                register_accepted_paper(record_id)
            else:
                print(f"  Could not derive record_id from request {request_id} — registry not updated.")
        except scrubber.ScrubLeak as e:
            print(f"  Accept succeeded on Zenodo BUT scrub leak blocked publication: {e}")
            _fire_pain(
                title="ICSAC Pipeline: Review Scrub Leak",
                body=(
                    f"Zenodo accept succeeded for request {request_id} but the "
                    f"scrubber blocked publication: {e}. The Zenodo acceptance "
                    f"is in effect; the landing page + public review are NOT "
                    f"published. Inspect the raw review, edit out the leak, "
                    f"then rerun `python3 scrubber.py {record_id or '<id>'}` "
                    f"and commit manually."
                ),
            )
        except Exception as e:
            print(f"  Accept succeeded on Zenodo but registry update failed: {e}")
            print(f"  (paper is accepted; add to {REGISTRY_PATH} manually)")
            _fire_pain(
                title="ICSAC Pipeline: Registry Push Failed",
                body=(f"Zenodo accept succeeded for request {request_id} but the "
                      f"icsacinstitute.org landing-page registry update failed: {e}. "
                      f"Paper is accepted on Zenodo; add the entry to "
                      f"{REGISTRY_PATH} manually to publish the landing page."),
            )
    return ok


def _fire_pain(title: str, body: str) -> None:
    """Direct ntfy /pain POST to the orchestrator. Best-effort, never raises."""
    try:
        req = urllib.request.Request(
            "http://100.117.63.73:8090/pain", data=body.encode()
        )
        req.add_header("Title", title)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def decline_request(request_id: str, comment: str = "",
                    review_data: dict | None = None,
                    review_summary: str = "",
                    specific_concerns: str = "") -> bool:
    """Decline a community inclusion request.

    If review_data is supplied, an ICSAC-branded decline comment is rendered
    with the review summary + concerns and posted with the action. Zenodo
    notifies the author via its own email machinery.
    """
    if review_data and not comment:
        import email_render
        comment = email_render.render_decline_comment(
            review_data, review_summary=review_summary,
            specific_concerns=specific_concerns,
        )
    return _action_request(request_id, "decline", comment)


# Backwards-compatible alias for any caller still using the old name.
reject_request = decline_request


def _action_request(request_id: str, action: str, comment: str) -> bool:
    """POST an action (accept/decline) on a community request."""
    url = f"{config.ZENODO_API}/requests/{request_id}/actions/{action}"
    payload = {}
    if comment:
        payload["payload"] = {"content": comment}

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.URLError as e:
        print(f"  Error {action}ing request {request_id}: {e}")
        return False


def post_request_comment(request_id: str, content: str,
                         fmt: str = "html") -> bool:
    """POST a comment to a Zenodo request.

    Used when the curator already accepted/declined via the Zenodo UI and we
    need to add our branded follow-up message after the fact. Zenodo notifies
    request participants (including the author) by email on new comments.

    `fmt` defaults to "html" because Zenodo's notification renderer treats
    "html" payloads as rich text with markdown-style formatting; the markdown
    we render flows through cleanly.
    """
    url = f"{config.ZENODO_API}/requests/{request_id}/comments"
    payload = {"payload": {"content": content, "format": fmt}}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.URLError as e:
        print(f"  Error posting comment to {request_id}: {e}")
        return False


def _get_request_record_id(request_id: str) -> str | None:
    """Look up the Zenodo record ID associated with a community request."""
    url = f"{config.ZENODO_API}/requests/{request_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            topic = data.get("topic", {}) or {}
            record = topic.get("record") or topic.get("record_id")
            if isinstance(record, dict):
                record = record.get("id")
            return str(record) if record else None
    except Exception as e:
        print(f"  _get_request_record_id failed: {e}")
        return None


def _fetch_record(record_id: str) -> dict:
    url = f"{config.ZENODO_API}/records/{record_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {config.ZENODO_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _extract_registry_entry(record_id: str, metadata: dict,
                            *, source: str = "zenodo-community") -> dict:
    """Shape a Zenodo record dict into the publications-registry schema.

    Returns a proto-entry suitable for publications.upsert_entry — slug
    + accepted_date are filled in by the upsert helper.
    """
    m = metadata.get("metadata", metadata)
    # Zenodo returns the description as HTML (tags + entity-escaped glyphs).
    # Strip tags first, THEN html.unescape so &nbsp;/&mdash;/&amp; collapse
    # to their literal characters — Astro's {} interpolation then renders
    # them as proper text instead of leaking escape sequences to the reader.
    raw_desc = m.get("description", "") or ""
    abstract = html.unescape(re.sub(r"<[^>]+>", "", raw_desc)).strip()
    abstract = re.sub(r"[ \t]+", " ", abstract)  # collapse whitespace runs from former &nbsp; etc.
    authors = []
    for c in m.get("creators", []):
        name = c.get("name", c.get("person_or_org", {}).get("name", "Unknown"))
        if "," in name:
            last, after = [s.strip() for s in name.split(",", 1)]
            name = f"{after} {last}".strip() if after else last
        authors.append(name)
    return {
        "record_id": str(record_id),
        "title": m.get("title", "Untitled"),
        "authors": authors or ["Unknown"],
        "doi": m.get("doi", f"10.5281/zenodo.{record_id}"),
        "abstract": abstract[:2000] if abstract else "",
        "source": source,
        "source_ref": f"https://zenodo.org/records/{record_id}",
    }


def _publish_public_review(record_id: str) -> str | None:
    """Scrub the internal review and stage it at public-reviews/<id>.md.

    Returns the path written, or None if no internal review exists yet.
    Raises scrubber.ScrubLeak when a forbidden token slips through; the
    caller (accept_request) converts that to a /pain signal.
    """
    reviews_dir = getattr(config, "REVIEWS_DIR", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "reviews"
    ))
    if not os.path.isdir(reviews_dir):
        return None
    present = [
        f for f in os.listdir(reviews_dir)
        if f.startswith(f"{record_id}_")
        and f.endswith(".md")
        and not f.endswith("_review_quality_control.md")
    ]
    if not present:
        print(f"  No internal review for {record_id}; public review not staged.")
        return None
    return scrubber.publish_public_review(record_id, reviews_dir, WEBSITE_REPO)


def _publish_public_rqc(record_id: str) -> str | None:
    """Scrub the internal RQC audit and stage its redacted public twin.

    Returns the path written, or None if no RQC file exists yet (older
    reviews pre-RQC-rollout). Raises scrubber.ScrubLeak if any forbidden
    token — including a reference to the redacted injection_indicators
    dimension — survives. The caller converts that to a /pain signal.
    """
    reviews_dir = getattr(config, "REVIEWS_DIR", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "reviews"
    ))
    if not os.path.isdir(reviews_dir):
        return None
    return scrubber.publish_public_rqc(record_id, reviews_dir, WEBSITE_REPO)


def register_accepted_paper(record_id: str) -> None:
    """Append/update the publications registry, stage the scrubbed review, push.

    Order:
      1) Fetch Zenodo metadata + upsert registry entry in accepted.json
      2) Scrub internal review → src/data/public-reviews/<id>.md (gated)
      3) Scrub internal RQC → src/data/public-reviews/<id>_review_quality_control.md (gated)
      4) Refresh panel stats snapshot
      5) git add all, commit, pull --rebase, push
    """
    metadata = _fetch_record(record_id)
    proto = _extract_registry_entry(record_id, metadata,
                                    source="zenodo-community")
    entry = publications.upsert_entry(proto)

    review_path = _publish_public_review(record_id)
    rqc_path = _publish_public_rqc(record_id)

    stats_path = _refresh_panel_stats()

    publications.commit_and_push(
        message=f"accepted: {entry['title']} ({record_id})",
        extra_paths=[review_path, rqc_path, stats_path],
    )
    print(
        f"  Registered paper {record_id} -> "
        f"{publications.publications_url(entry['slug'])} "
        f"(legacy /accepted/{record_id} also live)"
    )
    if review_path:
        print(f"  Scrubbed review staged at {review_path}")
    if rqc_path:
        print(f"  Scrubbed RQC staged at {rqc_path}")
    if stats_path:
        print(f"  Panel stats snapshot refreshed at {stats_path}")


def _refresh_panel_stats() -> str | None:
    """Regenerate the /stats snapshot. Non-fatal on failure."""
    reviews_dir = getattr(
        config,
        "REVIEWS_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviews"),
    )
    out = os.path.join(WEBSITE_REPO, "src/data/stats.json")
    try:
        return stats_mod.write_stats(reviews_dir, out)
    except Exception as e:
        print(f"  panel stats refresh failed (non-fatal): {e}")
        return None


