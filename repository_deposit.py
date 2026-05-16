"""Zenodo deposit step for the ICSAC submission pipeline (Option A — draft-only).

When a PDF-route submission is accepted by the panel and the author
checked the deposit_consent box on intake, ICSAC stages a DRAFT deposit
under the institute's own account using ZENODO_TOKEN. The deposit is
NOT published — no DOI is minted, no record goes live, no community
membership fires until an operator manually publishes the draft from
Zenodo's UI (or via `publish_draft` below).

This deliberate two-step model exists so the operator can sanity-check
each accepted manuscript's metadata in Zenodo before the DOI becomes
permanent. Once a DOI is minted it cannot be unminted; drafts can be
edited or discarded freely.

The deposit JSON is built from submission.json metadata (creators,
resource_type, publication_date, subject, funding, related_identifiers,
license, abstract, keywords, title) plus the paper.pdf the worker has
on disk.

The module deliberately uses urllib + json rather than `requests` so it
matches the rest of the pipeline's HTTP convention (review.py,
citation_*) and stays import-light. The only external dep is the
`markdown` package used for the abstract -> HTML conversion that
Zenodo's `description` field expects.
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import config


ZENODO_RESOURCE_TYPE = {
    "preprint":  ("publication", "preprint"),
    "article":   ("publication", "article"),
    "report":    ("publication", "report"),
    "dataset":   ("dataset", None),
    "software":  ("software", None),
    "other":     ("other", None),
}


class DepositFailed(RuntimeError):
    """Raised when the Zenodo deposit pipeline can't reach a published
    record. The worker catches this and falls back to the pending-copy
    accept email so the author still hears the decision; deposit can be
    retried manually from the saved submission.json."""


def _request_json(method: str, url: str, *, token: str,
                  body: Optional[dict] = None,
                  raw_body: Optional[bytes] = None,
                  content_type: str = "application/json",
                  timeout: int = 60) -> dict:
    """Thin urllib wrapper. Raises DepositFailed on HTTP errors with the
    response body included so the worker can audit-log a useful reason."""
    if body is not None and raw_body is not None:
        raise ValueError("pass body OR raw_body, not both")
    data: Optional[bytes] = raw_body
    if body is not None:
        data = _json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if not payload:
                return {}
            return _json.loads(payload.decode())
    except urllib.error.HTTPError as e:
        body_excerpt = e.read()[:500].decode(errors="replace")
        raise DepositFailed(
            f"Zenodo {method} {url} -> HTTP {e.code}: {body_excerpt}"
        ) from e
    except Exception as e:
        raise DepositFailed(f"Zenodo {method} {url} -> {type(e).__name__}: {e}") from e


def _build_metadata(submission: dict) -> dict:
    """Translate submission.json into a Zenodo deposit metadata dict.

    Form-captured fields map straight through; resource_type splits into
    Zenodo's upload_type + publication_type pair. Affiliations and ORCIDs
    on creators are passed when present. The abstract goes through
    `markdown` to HTML since Zenodo's description field renders HTML.
    """
    title = submission["title"]
    abstract_md = submission.get("abstract") or ""
    keywords = submission.get("keywords") or []
    license_id = submission.get("license") or "cc-by-4.0"
    publication_date = submission.get("publication_date") or ""
    resource_type = (submission.get("resource_type") or "preprint").lower()
    subject = submission.get("subject") or ""
    funding = submission.get("funding") or ""
    related = submission.get("related_identifiers") or []
    creators_in = submission.get("creators") or []

    upload_type, publication_type = ZENODO_RESOURCE_TYPE.get(
        resource_type, ZENODO_RESOURCE_TYPE["preprint"]
    )

    creators: list[dict] = []
    for c in creators_in:
        if isinstance(c, str):
            creators.append({"name": c})
            continue
        entry: dict[str, Any] = {"name": c.get("name", "").strip()}
        if c.get("orcid"):
            entry["orcid"] = c["orcid"]
        if c.get("affiliation"):
            entry["affiliation"] = c["affiliation"]
        creators.append(entry)
    if not creators:
        raise DepositFailed("submission has no creators — cannot mint a Zenodo record")

    try:
        import markdown as _md
        description_html = _md.markdown(abstract_md, extensions=["extra"])
    except Exception:
        # Fall back to wrapping in <p> tags if markdown lib unavailable
        description_html = "<p>" + abstract_md.replace("\n\n", "</p><p>") + "</p>"

    metadata: dict[str, Any] = {
        "title": title,
        "upload_type": upload_type,
        "description": description_html,
        "creators": creators,
        "publication_date": publication_date,
        "license": license_id,
        "access_right": "open",
        # The submitter explicitly authorizes ICSAC to deposit on their
        # behalf via deposit_consent on intake; auto-add to the icsac
        # community is the contract.
        "communities": [{"identifier": config.COMMUNITY_ID}],
    }
    if publication_type:
        metadata["publication_type"] = publication_type
    if keywords:
        metadata["keywords"] = list(keywords)
    if subject:
        metadata["subjects"] = [{"term": subject, "scheme": "ICSAC"}]
    if funding:
        # Free-text funding goes into notes since structured `grants`
        # require a Zenodo grant lookup ID. We can promote later if the
        # operator sets up a grant taxonomy mapping.
        metadata["notes"] = f"Funding: {funding}"
    if related:
        # Pass the form-captured shape straight through — RELATION_TYPES
        # were chosen to match Zenodo's vocabulary.
        metadata["related_identifiers"] = [
            {"identifier": r["identifier"], "relation": r["relation"]}
            for r in related
        ]

    return metadata


def stage_deposit_draft(submission: dict, paper_pdf_path: Path,
                         *, log=None, sandbox: bool = False) -> dict | None:
    """Stage a DRAFT Zenodo deposit for the submission. Does NOT publish.

    Returns {record_id, draft_url} on success; raises DepositFailed if
    any step fails. No DOI is minted at this stage — the draft sits in
    Zenodo's deposit dashboard waiting for operator review and a manual
    publish (via the Zenodo UI or `publish_draft` below).

    The optional `log` callable receives one-line progress strings —
    plumb the worker's _log function through so journalctl shows the
    deposit lifecycle alongside review/RQC/email lifecycle messages.

    `sandbox=True` (Tier 3 test path): use https://sandbox.zenodo.org and
    the ZENODO_SANDBOX_TOKEN env var instead of the production credentials.
    Drafts created there cannot become production DOIs and cost nothing
    real. If ZENODO_SANDBOX_TOKEN is unset, the deposit is SKIPPED with a
    warning logged via `log` and None returned, so a T3 smoke test can
    run end-to-end without sandbox credentials wired up.
    """
    import os as _os
    def _info(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg)

    if sandbox:
        token = _os.environ.get("ZENODO_SANDBOX_TOKEN", "").strip()
        api = "https://sandbox.zenodo.org/api"
        if not token:
            _info("  deposit-draft: SKIPPED — ZENODO_SANDBOX_TOKEN not set "
                  "(T3 sandbox path; configure to exercise the full deposit).")
            return None
    else:
        token = config.ZENODO_TOKEN
        api = config.ZENODO_API
        if not token:
            raise DepositFailed("ZENODO_TOKEN not configured")
    if not paper_pdf_path.is_file():
        raise DepositFailed(f"paper.pdf missing at {paper_pdf_path}")

    metadata = _build_metadata(submission)

    _info("  deposit-draft: creating empty deposition...")
    created = _request_json("POST", f"{api}/deposit/depositions",
                             token=token, body={})
    deposit_id = created["id"]
    bucket_url = created.get("links", {}).get("bucket")
    if not bucket_url:
        raise DepositFailed(f"deposition {deposit_id} response had no bucket URL")
    _info(f"  deposit-draft: id={deposit_id}, uploading paper.pdf...")

    pdf_bytes = paper_pdf_path.read_bytes()
    _request_json("PUT", f"{bucket_url}/paper.pdf",
                  token=token, raw_body=pdf_bytes,
                  content_type="application/octet-stream",
                  timeout=240)

    _info("  deposit-draft: setting metadata...")
    saved = _request_json("PUT", f"{api}/deposit/depositions/{deposit_id}",
                           token=token, body={"metadata": metadata})

    record_id = str(saved.get("id") or deposit_id)
    # Operator-facing draft URL. `links.html` points at the legacy deposit
    # editor; `links.self_html` points at the new uploads/<id> editor on
    # newer Zenodo deployments. Prefer self_html when present, fall back.
    draft_url = (
        saved.get("links", {}).get("self_html")
        or saved.get("links", {}).get("html")
        or f"https://zenodo.org/uploads/{record_id}"
    )
    _info(f"  deposit-draft: staged record_id={record_id} draft_url={draft_url}")
    _info("  deposit-draft: NOT published — operator must review + publish "
          "manually before the DOI is minted.")

    return {"record_id": record_id, "draft_url": draft_url}


def publish_draft(record_id: str, *, log=None) -> dict:
    """Publish a previously-staged draft deposit. Mints the DOI, makes the
    record live, triggers icsac-community membership.

    Operator-driven entry point. Not called from the worker. Use this
    after sanity-checking the staged metadata in Zenodo's UI.
    Returns {doi, record_url, record_id} on success; raises DepositFailed
    on error.
    """
    def _info(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg)

    token = config.ZENODO_TOKEN
    api = config.ZENODO_API
    if not token:
        raise DepositFailed("ZENODO_TOKEN not configured")

    _info(f"  deposit-publish: publishing record_id={record_id}...")
    published = _request_json(
        "POST", f"{api}/deposit/depositions/{record_id}/actions/publish",
        token=token,
    )

    doi = (
        published.get("doi")
        or published.get("metadata", {}).get("doi")
        or ""
    )
    final_id = str(published.get("record_id") or record_id)
    record_url = (
        published.get("links", {}).get("record_html")
        or f"https://zenodo.org/records/{final_id}"
    )
    if not doi:
        raise DepositFailed(
            f"deposition {record_id} published but response had no DOI: "
            f"{_json.dumps(published)[:300]}"
        )
    _info(f"  deposit-publish: live doi={doi} url={record_url}")

    return {"doi": doi, "record_url": record_url, "record_id": final_id}
