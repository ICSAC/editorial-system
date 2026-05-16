"""ICSAC publications registry — single source of truth for /publications.

Maintains src/data/accepted.json on the icsacinstitute.org repo. Three
writers populate the registry:

  - Zenodo community watcher (action.register_accepted_paper) — accepts
    in the icsac Zenodo community.
  - Submission intake DOI route (icsac-submission-intake/) — papers
    submitted via author-supplied DOI and accepted by the panel.
  - Submission intake PDF route post-publish (publish_watcher) — operator
    publishes the staged Zenodo draft and the watcher registers the now-
    live DOI.

Every entry powers /publications/<slug>; entries with `record_id` also
power the legacy /accepted/<record_id> share landings.

Filename note: the on-disk file is still `accepted.json` for back-compat
with TS imports that already reference it; semantically it's the
publications registry.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from typing import Any, Optional


WEBSITE_REPO = os.environ.get("ICSAC_WEBSITE_REPO", "")
REGISTRY_PATH = os.path.join(WEBSITE_REPO, "src/data/accepted.json")
PUBLICATIONS_BASE_URL = "https://icsacinstitute.org/publications"

VALID_SOURCES = {"zenodo-community", "submission-doi", "submission-pdf"}


def make_slug(title: str, existing_slugs: Optional[set[str]] = None) -> str:
    """Slugify a title to a kebab-case URL fragment.

    Splits on first colon or em/en-dash so subtitles don't bloat the URL,
    then lowercases + reduces non-alphanumerics to single hyphens. Caps
    at 80 chars. On collision with `existing_slugs`, appends -2, -3, ...
    """
    src = (title or "").strip()
    if not src:
        src = "paper"
    base = re.split(r"\s*[:—–]\s*", src, maxsplit=1)[0].strip() or src
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    slug = slug[:80].rstrip("-") or "paper"
    if not existing_slugs:
        return slug
    candidate = slug
    n = 2
    while candidate in existing_slugs:
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def publications_url(slug: str) -> str:
    return f"{PUBLICATIONS_BASE_URL}/{slug}"


def _load_registry() -> list[dict]:
    if not os.path.exists(REGISTRY_PATH):
        raise FileNotFoundError(f"Registry missing: {REGISTRY_PATH}")
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def _save_registry(registry: list[dict]) -> None:
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _match_existing(registry: list[dict], proto: dict) -> Optional[int]:
    """Find an existing registry entry by record_id then doi. Returns its index or None."""
    rid = proto.get("record_id")
    doi = proto.get("doi")
    for i, e in enumerate(registry):
        if rid and e.get("record_id") == rid:
            return i
        if doi and e.get("doi") == doi:
            return i
    return None


def upsert_entry(proto: dict) -> dict:
    """Insert or update a publications entry. Returns the final entry.

    `proto` must carry: title, authors (list[str]), doi, source. Optional:
    abstract, source_ref, record_id, accepted_date (defaults to today),
    slug (auto-derived from title if absent).

    Existing entries (matched by record_id or doi) are updated in place;
    the existing slug is preserved for URL stability. New entries get a
    fresh slug, deduped against the current registry.

    Caller is responsible for staging any ancillary files (public-review
    HTML, etc.) and then calling commit_and_push().

    Returns an empty dict when ICSAC_WEBSITE_REPO is not configured (the
    Zenodo accept itself still proceeds; the registry publish is skipped).
    """
    if not WEBSITE_REPO:
        print("  Registry publish skipped: ICSAC_WEBSITE_REPO not configured")
        return {}
    if proto.get("source") not in VALID_SOURCES:
        raise ValueError(
            f"invalid source {proto.get('source')!r}; want one of {sorted(VALID_SOURCES)}"
        )
    if not proto.get("title"):
        raise ValueError("proto.title is required")
    if not proto.get("doi"):
        raise ValueError("proto.doi is required")
    if not proto.get("authors"):
        raise ValueError("proto.authors must be a non-empty list")

    registry = _load_registry()
    existing_idx = _match_existing(registry, proto)

    final: dict[str, Any] = {}
    if existing_idx is not None:
        prior = registry[existing_idx]
        final["slug"] = prior.get("slug") or make_slug(
            proto["title"],
            {e.get("slug") for e in registry if e is not prior and e.get("slug")},
        )
        final["accepted_date"] = (
            proto.get("accepted_date")
            or prior.get("accepted_date")
            or datetime.date.today().isoformat()
        )
    else:
        existing_slugs = {e.get("slug") for e in registry if e.get("slug")}
        final["slug"] = proto.get("slug") or make_slug(proto["title"], existing_slugs)
        final["accepted_date"] = (
            proto.get("accepted_date") or datetime.date.today().isoformat()
        )

    if proto.get("record_id"):
        final["record_id"] = str(proto["record_id"])
    final["title"] = proto["title"]
    final["authors"] = list(proto["authors"])
    final["doi"] = proto["doi"]
    final["source"] = proto["source"]
    if proto.get("source_ref"):
        final["source_ref"] = proto["source_ref"]
    if proto.get("abstract"):
        final["abstract"] = proto["abstract"]

    # Re-key in canonical insert order so the JSON stays diff-friendly.
    ordered_keys = [
        "slug", "record_id", "title", "authors", "doi",
        "accepted_date", "source", "source_ref", "abstract",
    ]
    canonical = {k: final[k] for k in ordered_keys if k in final}

    if existing_idx is not None:
        registry[existing_idx] = canonical
    else:
        registry.append(canonical)

    _save_registry(registry)
    return canonical


def stage_public_review_for_slug(
    review_key: str,
    slug: str,
    reviews_dir: str,
) -> tuple[Optional[str], Optional[str]]:
    """Redact the panel review + RQC keyed by `review_key`, then rename
    the generated public-reviews/<key>.{md,html} files to <slug>.{md,html}
    so /publications/<slug> can find them.

    `review_key` is the prefix redaction.publish_public_review searches
    for under reviews_dir — record_id for Zenodo-watcher-path papers,
    sub_id (e.g. ICSAC-SUB-00006) for intake-path papers.

    Returns (review_md_path, rqc_md_path) — either may be None if no
    matching review was found. RedactionLeak from the underlying redaction
    bubbles up; callers gate it the same way action.accept_request does.

    Returns (None, None) when ICSAC_WEBSITE_REPO is not configured.
    """
    if not WEBSITE_REPO:
        print("  Public-review stage skipped: ICSAC_WEBSITE_REPO not configured")
        return (None, None)
    import redaction  # editorial system module
    review_md_orig = redaction.publish_public_review(
        review_key, reviews_dir, WEBSITE_REPO,
    )
    rqc_md_orig = redaction.publish_public_rqc(
        review_key, reviews_dir, WEBSITE_REPO,
    )

    out_dir = os.path.join(WEBSITE_REPO, "src", "data", "public-reviews")

    def _rename_pair(orig_md: Optional[str], src_base: str, dst_base: str) -> Optional[str]:
        if not orig_md or src_base == dst_base:
            return orig_md
        final_md: Optional[str] = None
        for ext in (".md", ".html"):
            src = os.path.join(out_dir, f"{src_base}{ext}")
            dst = os.path.join(out_dir, f"{dst_base}{ext}")
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
            if ext == ".md":
                final_md = dst
        return final_md

    final_review = _rename_pair(review_md_orig, review_key, slug)
    final_rqc = _rename_pair(
        rqc_md_orig,
        f"{review_key}_review_quality_control",
        f"{slug}_review_quality_control",
    )
    return final_review, final_rqc


def commit_and_push(message: str, extra_paths: Optional[list[str]] = None) -> None:
    """Stage accepted.json (+ any extras), commit, pull --rebase, push.

    No-op when the working tree is clean. Best-effort `git pull --rebase`;
    push failures raise so callers can surface a /pain signal.

    No-op when ICSAC_WEBSITE_REPO is not configured.
    """
    if not WEBSITE_REPO:
        return
    def run(*cmd, check=True):
        return subprocess.run(
            cmd, cwd=WEBSITE_REPO, capture_output=True, text=True, check=check
        )

    run("git", "add", "src/data/accepted.json")
    for p in extra_paths or []:
        if not p:
            continue
        if os.path.isabs(p):
            rel = os.path.relpath(p, WEBSITE_REPO)
        else:
            rel = p
        full = os.path.join(WEBSITE_REPO, rel)
        if os.path.exists(full):
            run("git", "add", rel)
            # If the path is a markdown file, also stage the sibling .html
            # (redaction writes pairs).
            if rel.endswith(".md"):
                html_rel = rel[:-3] + ".html"
                if os.path.exists(os.path.join(WEBSITE_REPO, html_rel)):
                    run("git", "add", html_rel)

    status = run("git", "status", "--porcelain").stdout
    if not status.strip():
        return
    run("git", "commit", "-m", message)
    try:
        run("git", "pull", "--rebase", "--autostash", "origin", "main")
    except subprocess.CalledProcessError as e:
        print(f"  git pull --rebase warning: {e.stderr.strip()}")
    run("git", "push", "origin", "HEAD:main")
