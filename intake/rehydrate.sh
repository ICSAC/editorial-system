#!/bin/bash
# Operator CLI: rehydrate a stubbed DOI submission's paper.pdf.
# Usage: ./rehydrate.sh ICSAC-SUB-NNNNN
set -euo pipefail
INTAKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$INTAKE_DIR/../.venv/bin/python" "$INTAKE_DIR/rehydrate.py" "$@"
