#!/bin/bash
# Operator CLI: register a staged ICSAC DOI with Crossref.
#   ./register-doi.sh ICSAC-SUB-NNNNN          -> Crossref TEST system (nothing real, nothing public)
#   ./register-doi.sh ICSAC-SUB-NNNNN --live   -> doi.crossref.org: permanent DOI, /publications push, author email
# Staging (the draft) happens on `decide.sh <id> accept`; re-stage by hand with
#   ../.venv/bin/python ../crossref_deposit.py stage ICSAC-SUB-NNNNN
set -euo pipefail
INTAKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
. "$HOME/.config/zenodo-pipeline.env"
[ -f "$HOME/.config/icsac-intake.env" ] && . "$HOME/.config/icsac-intake.env"
[ -f "$HOME/.config/crossref.env" ] && . "$HOME/.config/crossref.env"
# The /publications push needs the website checkout; publications.py reads only
# the environment (first --live, 2026-09-29, skipped the landing page without it).
ICSAC_WEBSITE_REPO="${ICSAC_WEBSITE_REPO:-$HOME/Desktop/icsac/icsacinstitute.org}"
set +a
exec "$INTAKE_DIR/../.venv/bin/python" "$INTAKE_DIR/../crossref_deposit.py" register "$@"
