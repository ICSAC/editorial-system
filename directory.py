"""Community member privacy and directory rendering rules.

Applies opt-in-only directory listing based on Google Form signup preferences.
Never exposes information the member did not explicitly consent to.
"""

DIRECTORY_CHOICES = {
    "public": "Yes, list me publicly",
    "minimal": "Yes, but name and role only (no contact info)",
    "private": "No — keep me private",
}

CONTACT_FIELDS = {
    "email": "Email address",
    "orcid": "ORCID",
    "scholar": "Google Scholar profile",
    "website": "Personal website",
    "alias": "ICSAC-aliased email only (we forward to your real address)",
}


def directory_entry(member: dict) -> dict | None:
    """Build a directory entry that respects the member's privacy choices.

    Returns None for private members. For public/minimal members, returns only
    the fields they consented to expose. Never includes email unless they ticked
    'Email address' explicitly.
    """
    choice = member.get("directory_choice", "")

    if choice == DIRECTORY_CHOICES["private"]:
        return None

    entry = {
        "display_name": format_display_name(member),
        "affiliation": member.get("affiliation", ""),
        "role": member.get("contribution_role", ""),
        "research_interests": member.get("research_interests", []),
    }

    if choice == DIRECTORY_CHOICES["minimal"]:
        entry.pop("affiliation", None)
        return entry

    consented = set(member.get("public_contact_fields", []))
    contact = {}
    if CONTACT_FIELDS["email"] in consented:
        contact["email"] = member.get("email", "")
    if CONTACT_FIELDS["alias"] in consented:
        contact["email_alias"] = member.get("icsac_alias", "")
    if CONTACT_FIELDS["orcid"] in consented:
        contact["orcid"] = member.get("orcid", "")
    if CONTACT_FIELDS["scholar"] in consented:
        contact["scholar"] = member.get("scholar_url", "")
    if CONTACT_FIELDS["website"] in consented:
        contact["website"] = member.get("website_url", "")

    if contact:
        entry["contact"] = contact

    return entry


def format_display_name(member: dict) -> str:
    """Build a display name from title preference + name + post-nominals."""
    title = member.get("title", "")
    name = member.get("full_name", "")
    postnoms = member.get("post_nominals", "")

    parts = []
    if title and title not in ("No title (first name is fine)", "Prefer not to say"):
        parts.append(title)
    parts.append(name)

    display = " ".join(parts)
    if postnoms:
        display = f"{display}, {postnoms}"
    return display


def public_directory(members: list[dict]) -> list[dict]:
    """Filter the member list into directory-visible entries only."""
    entries = []
    for m in members:
        e = directory_entry(m)
        if e is not None:
            entries.append(e)
    return entries
