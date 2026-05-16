#!/bin/bash
# Curator CLI: apply a decision verdict to a borderline submission.
# Activates the repo-root venv and forwards args to apply_decision.py.
set -euo pipefail
INTAKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$INTAKE_DIR/../.venv/bin/python" "$INTAKE_DIR/apply_decision.py" "$@"
