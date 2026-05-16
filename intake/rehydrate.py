"""Rehydrate a stubbed DOI submission's paper.pdf from its resolver.

Usage:
    .venv/bin/python rehydrate.py ICSAC-SUB-NNNNN

If paper.pdf already exists, no-op. If paper.pdf.url stub exists, refetch
from the resolver (Zenodo or arXiv based on DOI shape), verify sha256
against the stub, write paper.pdf in place. Stub remains alongside the
PDF so the submission's lazy-state is observable.
"""

from __future__ import annotations

import hashlib
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

import submission_intake as ingest  # noqa: E402

SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
SUB_ID_RE = re.compile(r"^ICSAC-SUB-\d{5}$")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: rehydrate.py ICSAC-SUB-NNNNN", file=sys.stderr)
        return 2
    sub_id = argv[1].strip()
    if not SUB_ID_RE.match(sub_id):
        print(f"bad sub id: {sub_id!r}", file=sys.stderr)
        return 2

    sub_dir = SUBMISSIONS_ROOT / sub_id
    if not sub_dir.is_dir():
        print(f"no such submission: {sub_dir}", file=sys.stderr)
        return 1

    paper = sub_dir / "paper.pdf"
    stub = sub_dir / "paper.pdf.url"

    if paper.exists():
        print(f"{sub_id}: paper.pdf already present ({paper.stat().st_size} bytes)")
        return 0
    if not stub.exists():
        print(f"{sub_id}: neither paper.pdf nor paper.pdf.url present", file=sys.stderr)
        return 1

    info = json.loads(stub.read_text())
    doi = info.get("doi") or ""
    expected_sha = info.get("sha256") or ""
    expected_size = int(info.get("size_bytes") or 0)
    if not (doi and expected_sha):
        print(f"{sub_id}: stub is missing doi or sha256", file=sys.stderr)
        return 1

    print(f"{sub_id}: rehydrating from {doi}...")
    if ingest.is_arxiv_ref(doi):
        arxiv_id = ingest.arxiv_ref_to_id(doi)
        downloaded = ingest.download_arxiv_pdf(arxiv_id, dest_dir=str(sub_dir))
    else:
        record_id = ingest.doi_to_record_id(doi)
        metadata = ingest.fetch_metadata(record_id)
        downloaded = ingest.download_pdf(metadata, dest_dir=str(sub_dir))

    if not downloaded:
        print(f"{sub_id}: resolver returned no PDF", file=sys.stderr)
        return 1

    Path(downloaded).rename(paper)
    actual_sha = hashlib.sha256(paper.read_bytes()).hexdigest()
    actual_size = paper.stat().st_size

    if actual_sha != expected_sha:
        print(
            f"{sub_id}: sha256 MISMATCH — expected {expected_sha}, "
            f"got {actual_sha}. The resolver returned different bytes than "
            f"were originally reviewed; the panel verdict is no longer "
            f"backed by these bytes. Leaving the file in place but flagging.",
            file=sys.stderr,
        )
        # Don't auto-delete; curator decides what to do. Exit non-zero so
        # automation can detect.
        return 3

    if actual_size != expected_size:
        print(
            f"{sub_id}: size mismatch — expected {expected_size}, "
            f"got {actual_size} (sha256 matched, size annotation drift)",
            file=sys.stderr,
        )

    print(
        f"{sub_id}: rehydrated {actual_size} bytes; sha256 verified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
