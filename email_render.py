"""Author correspondence: render accept/reject/invite email templates with author metadata."""

import os
import re
from urllib.parse import quote

import config
from review import _creator_display_names


def load_template(name: str) -> str:
    """Load an email template by name."""
    path = os.path.join(config.TEMPLATES_DIR, f"{name}.md")
    with open(path) as f:
        return f.read()


class TemplateUnfilledKeysError(RuntimeError):
    """Raised when a template still contains {{...}} placeholders after rendering.

    Hard-fail by design: author-facing mail with an unfilled key (e.g. a
    'Dear {{author_name}},' greeting) is worse than no mail at all. The
    raise propagates through the worker, which converts it to /pain via the
    standard nerve and writes a `template_unfilled_keys` audit-log entry.
    Caller should never catch and swallow this.
    """


def _render(template: str, data: dict) -> str:
    """Replace {{key}} placeholders with values from data.

    Hard-fails if any {{...}} remains after substitution — by design,
    silent template breakage in author-facing mail is worse than no mail.
    Missing keys are still left in place during the substitution pass
    (per existing semantics), but the post-pass scan catches them and
    raises before any byte is sent.
    """
    def sub(match):
        key = match.group(1).strip()
        return str(data.get(key, match.group(0)))
    rendered = re.sub(r"\{\{(\w+)\}\}", sub, template)
    leftover = re.findall(r"\{\{[^}]+\}\}", rendered)
    if leftover:
        raise TemplateUnfilledKeysError(
            f"unfilled template keys after render: {sorted(set(leftover))}"
        )
    return rendered


def _split_creator(entry: str) -> tuple[str, str]:
    """Best-effort (first, last) split for a Zenodo creator string.

    'Doe, Jane M.' -> ('Jane', 'Doe')
    'Jane M. Doe'  -> ('Jane', 'Doe')
    'Plato'                -> ('Plato', 'Plato')
    """
    entry = entry.strip()
    if "," in entry:
        last, after = [s.strip() for s in entry.split(",", 1)]
        first = after.split()[0].rstrip(".") if after else last
        return (first, last)
    parts = entry.split()
    if len(parts) >= 2:
        return (parts[0], parts[-1])
    return (entry, entry)


def _greeting(creators: list, title_pref: str) -> str:
    """Build the name used after 'Dear '.

    With a title preference, use 'Title Lastname' (e.g. 'Dr. Doe').
    Without one, use the author's first name. Title prefs are only available
    for authors who've opted into the community directory.
    """
    entry = creators[0] if creators else "Researcher"
    first, last = _split_creator(entry)
    if title_pref and title_pref not in ("No title (first name is fine)", "Prefer not to say", ""):
        return f"{title_pref} {last}"
    return first


def _share_urls(paper_title: str, share_target_url: str) -> dict:
    """Build pre-filled social-share URLs for the accept email.

    Share target is the ICSAC-branded landing page on icsacinstitute.org,
    not the raw Zenodo record. LinkedIn and Facebook scrape OpenGraph tags
    from the target URL, and the landing page's tags are ICSAC-branded so
    the preview card shows ICSAC rather than generic Zenodo.
    """
    share_sentence = (
        f'My paper "{paper_title}" was accepted into the ICSAC Community '
        f'— open peer review with AI tooling for complexity science.'
    )
    enc_sentence = quote(share_sentence, safe="")
    enc_url = quote(share_target_url, safe="")
    bluesky_text = quote(f"{share_sentence} {share_target_url}", safe="")
    return {
        "share_x_url": f"https://twitter.com/intent/tweet?text={enc_sentence}&url={enc_url}",
        "share_linkedin_url": f"https://www.linkedin.com/sharing/share-offsite/?url={enc_url}",
        "share_fb_url": f"https://www.facebook.com/sharer/sharer.php?u={enc_url}",
        "share_bluesky_url": f"https://bsky.app/intent/compose?text={bluesky_text}",
    }


def _base_data(review_data: dict) -> dict:
    creators = _creator_display_names(review_data.get("creators"))
    title_pref = review_data.get("author_title_preference", "")
    paper_title = review_data.get("title", "Untitled")
    record_id = review_data.get("record_id", "")
    zenodo_url = f"https://zenodo.org/records/{record_id}"
    site_base = getattr(config, "SITE_BASE_URL", "https://icsacinstitute.org")
    share_target_url = f"{site_base}/accepted/{record_id}" if record_id else zenodo_url
    # icsac_submission_id is the one canonical author-facing identifier — same
    # key the audit-log uses (sub_id field on the submission record). For the
    # Zenodo-watcher path, record_id is the Zenodo record ID; for the
    # icsac-submission-intake path, record_id is the ICSAC-SUB-NNNNN string.
    # Empty default rather than missing key — empty renders cleanly while a
    # missing key would leave the literal {{icsac_submission_id}} placeholder
    # and now (with the post-render assert) hard-fail the send.
    data = {
        "paper_title": paper_title,
        "author_name": ", ".join(creators) if creators else "Researcher",
        "greeting": _greeting(creators, title_pref),
        "icsac_submission_id": str(record_id) if record_id else "",
        "zenodo_record_url": zenodo_url,
        "share_target_url": share_target_url,
        "zenodo_submit_url": f"https://zenodo.org/communities/{getattr(config, 'COMMUNITY_ID', 'icsac')}",
        "google_form_url": getattr(config, "GOOGLE_FORM_URL", "https://icsacinstitute.org/join"),
    }
    data.update(_share_urls(paper_title, share_target_url))
    return data


def render_accept_email(review_data: dict, google_form_url: str = "") -> str:
    """Render the accept email."""
    template = load_template("accept")
    data = _base_data(review_data)
    if google_form_url:
        data["google_form_url"] = google_form_url
    return _render(template, data)


def render_revise_and_resubmit_email(review_data: dict, review_summary: str = "",
                                     specific_concerns: str = "") -> str:
    """Render the revise-and-resubmit email — ICSAC's default non-accept path.

    Used for engageable in-scope submissions whose issues revision could
    plausibly repair. The author is invited to revise and resubmit (no limit
    on rounds, no bias on re-evaluation).
    """
    template = load_template("revise-and-resubmit")
    data = _base_data(review_data)
    data["review_summary"] = review_summary or "Please see detailed review notes below."
    data["specific_concerns"] = specific_concerns or "Review details available upon request."
    return _render(template, data)


def render_scope_reject_email(review_data: dict, review_summary: str = "",
                            specific_concerns: str = "") -> str:
    """Render the scope-not-suitable rejection email.

    Reserved for submissions outside ICSAC's editorial scope (pseudoscience,
    non-engageable epistemics). This is NOT the standard decline path — for
    engageable in-scope work that needs revision, use
    `render_revise_and_resubmit_email` instead.
    """
    template = load_template("scope-reject")
    data = _base_data(review_data)
    data["review_summary"] = review_summary or "Please see detailed review notes below."
    data["specific_concerns"] = specific_concerns or "Review details available upon request."
    return _render(template, data)


def render_community_invite_email(review_data: dict, google_form_url: str = "") -> str:
    """Render the community invite (perks/signup) email sent after accept."""
    template = load_template("community-invite")
    data = _base_data(review_data)
    if google_form_url:
        data["google_form_url"] = google_form_url
    return _render(template, data)


def render_accept_comment(review_data: dict, landing_url: str = "") -> str:
    """Render the markdown comment we post to the Zenodo request on accept.

    The comment is delivered to the author by Zenodo's notification machinery,
    so it does not need a Subject line, Dear-greeting, or signature wrapper.
    Share links and rich content live on the icsacinstitute.org landing page;
    the comment just points there.
    """
    template = load_template("accept-comment")
    data = _base_data(review_data)
    record_id = review_data.get("record_id", "")
    site_base = getattr(config, "SITE_BASE_URL", "https://icsacinstitute.org")
    data["landing_url"] = landing_url or f"{site_base}/accepted/{record_id}"
    return _render(template, data)


def render_revise_and_resubmit_comment(review_data: dict, review_summary: str = "",
                                       specific_concerns: str = "") -> str:
    """Render the markdown comment we post to the Zenodo request on R&R.

    This is ICSAC's default decline path — engageable in-scope work that
    needs revision. Use `render_scope_reject_comment` for scope-not-suitable
    submissions.
    """
    template = load_template("revise-and-resubmit-comment")
    data = _base_data(review_data)
    data["review_summary"] = review_summary or "Please see review notes for details."
    data["specific_concerns"] = specific_concerns or "Review report available on request."
    return _render(template, data)


def render_scope_reject_comment(review_data: dict) -> str:
    """Render the markdown comment posted to the Zenodo request on scope-reject.

    Scope-not-suitable only. The scope-reject template does not carry a review
    summary or concerns list — the verdict is "out of scope," not "revise
    these points" — so the signature is intentionally minimal.
    """
    template = load_template("scope-reject-comment")
    data = _base_data(review_data)
    return _render(template, data)
