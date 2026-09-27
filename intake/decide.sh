#!/bin/bash
# Operator CLI: apply the curator verdict to a submission.
#   decide.sh ICSAC-SUB-NNNNN <accept|revise|scope_reject> [note]
# Runs apply_decision as a PACKAGE MODULE from the repo root. 2026-09-27: running the
# file directly died on its relative import (`from . import notify_author`) -- the
# first T2 rehearsal of a human decision found it. Env: the same files the worker uses.
set -euo pipefail
INTAKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$INTAKE_DIR/.." && pwd)"
set -a
[ -f "$HOME/.config/zenodo-pipeline.env" ] && . "$HOME/.config/zenodo-pipeline.env"
[ -f "$HOME/.config/icsac-intake.env" ] && . "$HOME/.config/icsac-intake.env"
[ -f "$HOME/.config/crossref.env" ] && . "$HOME/.config/crossref.env"
set +a
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m intake.apply_decision "$@"
