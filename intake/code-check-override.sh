#!/bin/bash
# Curator override for the code and data check: the paper was held for a
# missing code/data link and the flag was wrong. Records the override and
# queues the paper for the panel.
#   code-check-override.sh ICSAC-SUB-NNNNN
set -euo pipefail
INTAKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$INTAKE_DIR/.." && pwd)"
set -a
[ -f "$HOME/.config/zenodo-pipeline.env" ] && . "$HOME/.config/zenodo-pipeline.env"
[ -f "$HOME/.config/icsac-intake.env" ] && . "$HOME/.config/icsac-intake.env"
set +a
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m intake.code_check_override "$@"
