"""Curator override for the code and data check: the flag was wrong.

    code-check-override.sh ICSAC-SUB-NNNNN

Marks a submission the check held so the worker skips the check, then queues
it again so the panel reviews it. Refuses a paper that already has a decision
or that the check did not hold.
"""
from __future__ import annotations

import json
import sys

from . import submission_worker as worker
from .apply_decision import SUB_ID_RE


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not SUB_ID_RE.match(argv[1].strip()):
        print("usage: code-check-override.sh ICSAC-SUB-NNNNN", file=sys.stderr)
        return 2
    sub_id = argv[1].strip()
    sub_dir, tier = worker._resolve_sub_dir(sub_id)
    state_path = sub_dir / "state.json"
    if not state_path.exists():
        print(f"no such submission: {sub_dir}", file=sys.stderr)
        return 1
    state = json.loads(state_path.read_text())
    if state.get("decision"):
        print(f"{sub_id} already has a decision on record: {state['decision']}. Nothing done.",
              file=sys.stderr)
        return 3
    if state.get("precheck") != "code_data_link_missing":
        print(f"{sub_id} is not held by the code and data check. Nothing done.", file=sys.stderr)
        return 3
    worker._write_state(sub_dir, state="received", precheck="overridden",
                        code_check_override=True, code_check_override_at=worker._now_iso())
    test_mode = tier in (2, 3)
    queue = worker.TEST_QUEUE_DIR if test_mode else worker.QUEUE_DIR
    queue.mkdir(parents=True, exist_ok=True)
    (queue / sub_id).write_text(worker._now_iso())
    worker._audit({"sub_id": sub_id, "event": "code_check_override", "by": "curator"},
                  test_mode=test_mode)
    print(f"{sub_id}: override recorded; queued for the panel")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
