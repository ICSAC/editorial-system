#!/usr/bin/env python3
"""Fill the keywords on publications-registry entries (src/data/accepted.json).

Where each entry's keywords come from, first hit wins:
  1. the submission's own keywords (sub_id -> <submissions root>/<sub_id>/submission.json);
  2. the Zenodo record's keywords (record_id -> zenodo.org/api/records/<id>).

A dry run by default: it prints, per entry, the keywords it has and the ones it
would get. --write saves accepted.json in ICSAC_WEBSITE_REPO and requires
ICSAC_PUBLISH_APPROVED=1, because a pushed registry is a publication. It never
commits or pushes; the curator does that. Entries that already carry keywords
keep them unless --refresh is given.

    ICSAC_WEBSITE_REPO=<site checkout> python3 tools/backfill-keywords.py [--refresh] [--write]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publications  # noqa: E402

SUBMISSIONS_ROOT = Path(os.environ.get("ICSAC_SUBMISSIONS_ROOT", os.path.expanduser("~/icsac-submissions")))


def from_submission(sub_id: str) -> list[str]:
    path = SUBMISSIONS_ROOT / sub_id / "submission.json"
    if not path.exists():
        return []
    return publications.clean_keywords(json.loads(path.read_text()).get("keywords"))


def from_zenodo(record_id: str) -> list[str]:
    req = urllib.request.Request(f"https://zenodo.org/api/records/{record_id}",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        meta = json.load(r).get("metadata") or {}
    return publications.clean_keywords(meta.get("keywords"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="save accepted.json (needs ICSAC_PUBLISH_APPROVED=1)")
    ap.add_argument("--refresh", action="store_true", help="replace keywords an entry already has")
    args = ap.parse_args()
    if not publications.WEBSITE_REPO:
        print("set ICSAC_WEBSITE_REPO to the website checkout", file=sys.stderr)
        return 2
    if args.write and not publications.publish_approved():
        print("--write needs ICSAC_PUBLISH_APPROVED=1 (a pushed registry is a publication)", file=sys.stderr)
        return 2

    registry = publications._load_registry()
    changed = 0
    for entry in registry:
        have = entry.get("keywords") or []
        if have and not args.refresh:
            print(f"= {entry['slug']}: keeps {have}")
            continue
        new: list[str] = []
        where = ""
        if entry.get("sub_id"):
            new, where = from_submission(entry["sub_id"]), "submission"
        if not new and entry.get("record_id"):
            new, where = from_zenodo(entry["record_id"]), f"zenodo {entry['record_id']}"
        if not new:
            print(f"- {entry['slug']}: no keywords found")
            continue
        if new == have:
            print(f"= {entry['slug']}: unchanged ({where})")
            continue
        print(f"+ {entry['slug']} ({where}): {have or '[]'} -> {new}")
        entry["keywords"] = new
        changed += 1

    if args.write and changed:
        publications._save_registry(registry)
        print(f"\nwrote {changed} entr{'y' if changed == 1 else 'ies'} to {publications.REGISTRY_PATH}; "
              "nothing committed or pushed")
    else:
        print(f"\n{changed} entr{'y' if changed == 1 else 'ies'} would change"
              + ("" if args.write else " (dry run; --write to save)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
