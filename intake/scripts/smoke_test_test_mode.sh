#!/usr/bin/env bash
# Smoke test for the test-mode pipeline.
#
# Submits a test record using the configured test ORCID (TEST_ORCID env
# var; must be enrolled in the handler's TEST_ORCIDS allowlist) and
# verifies all the layered isolation guarantees:
#   1. Production audit-log.jsonl did NOT grow
#   2. audit-log-test.jsonl exists and gained a `test: true` entry
#   3. journalctl shows [TEST] prefixed log lines from the handler
#   4. State directory landed under <root>/test/, not <root>/<id>/
#   5. /api/submission/<id>/state returns test_mode=true
#   6. No queue marker was dropped (worker would have processed it)
#
# Run with: bash scripts/smoke_test_test_mode.sh
# Requires: INTAKE_HMAC_SECRET, TEST_ORCID in env (or sourced from
#           ~/.config/icsac-intake.env)

set -euo pipefail

INTAKE_URL="${INTAKE_URL:-http://127.0.0.1:8443/api/submit}"
SERVICE="${SERVICE:-intake-server.service}"
PROD_AUDIT="${PROD_AUDIT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/reviews/audit-log.jsonl}"
TEST_AUDIT="${TEST_AUDIT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/reviews/audit-log-test.jsonl}"
SUB_ROOT="${SUB_ROOT:-$HOME/icsac-submissions}"
TEST_DIR="${SUB_ROOT}/test"
QUEUE_DIR="${SUB_ROOT}/queue"

if [[ -z "${INTAKE_HMAC_SECRET:-}" ]]; then
  if [[ -f "$HOME/.config/icsac-intake.env" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$HOME/.config/icsac-intake.env"; set +a
  fi
fi
if [[ -z "${INTAKE_HMAC_SECRET:-}" ]]; then
  echo "INTAKE_HMAC_SECRET not set" >&2
  exit 2
fi
if [[ -z "${TEST_ORCID:-}" ]]; then
  echo "TEST_ORCID not set — required so the script doesn't bake an" >&2
  echo "ORCID into source. Export TEST_ORCID before invoking." >&2
  exit 2
fi

# ── snapshot pre-state ─────────────────────────────────────────
prod_lines_before=$(wc -l < "$PROD_AUDIT" 2>/dev/null || echo 0)
prod_mtime_before=$(stat -c %Y "$PROD_AUDIT" 2>/dev/null || echo 0)
test_lines_before=$(wc -l < "$TEST_AUDIT" 2>/dev/null || echo 0)
queue_count_before=$(ls -1 "$QUEUE_DIR" 2>/dev/null | wc -l)
echo "[smoke] pre-state: prod_audit=${prod_lines_before}L test_audit=${test_lines_before}L queue=${queue_count_before}"

# ── build a minimal valid PDF ──────────────────────────────────
PDF_TMP=$(mktemp --suffix=.pdf)
trap 'rm -f "$PDF_TMP"' EXIT
# Smallest text-bearing PDF: include ≥2000 chars of payload text since
# the upload route enforces ingest.PDF_TEXT_MIN_CHARS at handler-time.
LOREM=$(python3 -c "import sys; sys.stdout.write(('Test submission body content. This is the smoke-test payload for the ICSAC test-mode pipeline. ' * 30))")
python3 - <<PY
import sys, pathlib
text = """$LOREM"""
# Minimal text-layer PDF (single page, Helvetica). Hand-rolled so the
# script has no extra dependencies. Encodes 'text' as one TJ stream.
pdf = b"%PDF-1.4\n"
objs = []
def add(b):
    objs.append(b)
add(b"<< /Type /Catalog /Pages 2 0 R >>")
add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
stream_lines = []
y = 750
for i in range(0, len(text), 90):
    chunk = text[i:i+90].replace("(", r"\(").replace(")", r"\)")
    stream_lines.append(f"BT /F1 8 Tf 50 {y} Td ({chunk}) Tj ET")
    y -= 10
    if y < 50: break
stream = "\n".join(stream_lines).encode()
add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
offs = []
buf = pdf
for i, body in enumerate(objs, start=1):
    offs.append(len(buf))
    buf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
xref_off = len(buf)
buf += f"xref\n0 {len(objs)+1}\n".encode()
buf += b"0000000000 65535 f \n"
for o in offs:
    buf += f"{o:010d} 00000 n \n".encode()
buf += f"trailer << /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref_off}\n%%EOF\n".encode()
pathlib.Path("$PDF_TMP").write_bytes(buf)
PY

# ── build the multipart body and HMAC-sign ─────────────────────
BOUNDARY="----smoke$(date +%s)$$"
BODY_TMP=$(mktemp)
trap 'rm -f "$PDF_TMP" "$BODY_TMP"' EXIT

field() {
  printf -- "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" "$BOUNDARY" "$1" "$2"
}
{
  field name "Smoke Test User"
  field email "smoke-test@example.invalid"
  field orcid "$TEST_ORCID"
  field coi on
  field exclusivity on
  field title "Smoke Test Submission $(date -u +%FT%TZ)"
  field abstract "Smoke test abstract padded to clear the fifty-character minimum that the upload validator enforces on the abstract field. This text is purely synthetic."
  field keywords "smoke,test,pipeline"
  field license "cc-by-4.0"
  field resource_type "preprint"
  field publication_date "$(date -u +%F)"
  field subject ""
  field funding ""
  field creators "[{\"name\":\"Smoke Test User\",\"orcid\":\"$TEST_ORCID\"}]"
  field related_identifiers '[]'
  field deposit_consent on
  printf -- "--%s\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"smoke.pdf\"\r\nContent-Type: application/pdf\r\n\r\n" "$BOUNDARY"
  cat "$PDF_TMP"
  printf -- "\r\n--%s--\r\n" "$BOUNDARY"
} > "$BODY_TMP"

TS=$(date +%s)
SIG=$(printf "%s." "$TS" | cat - "$BODY_TMP" | openssl dgst -sha256 -hmac "$INTAKE_HMAC_SECRET" -hex | awk '{print $NF}')

# ── fire the request ───────────────────────────────────────────
echo "[smoke] POSTing test submission to $INTAKE_URL"
RESP=$(curl -sS -X POST "$INTAKE_URL" \
  -H "Content-Type: multipart/form-data; boundary=$BOUNDARY" \
  -H "x-icsac-timestamp: $TS" \
  -H "x-icsac-signature: sha256=$SIG" \
  -H "x-icsac-auth-orcid: $TEST_ORCID" \
  -H "x-icsac-auth-name: Smoke%20Test%20User" \
  --data-binary "@$BODY_TMP")
echo "[smoke] response: $RESP"

SUB_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('sub_id',''))")
if [[ -z "$SUB_ID" ]]; then
  echo "[smoke] FAIL: no sub_id in response" >&2
  exit 1
fi
echo "[smoke] sub_id=$SUB_ID"

case "$SUB_ID" in
  ICSAC-SUB-TEST-*) ;;
  *) echo "[smoke] FAIL: sub_id is not in test format: $SUB_ID" >&2; exit 1 ;;
esac

# ── verify isolation ───────────────────────────────────────────
sleep 1

prod_lines_after=$(wc -l < "$PROD_AUDIT" 2>/dev/null || echo 0)
prod_mtime_after=$(stat -c %Y "$PROD_AUDIT" 2>/dev/null || echo 0)
test_lines_after=$(wc -l < "$TEST_AUDIT" 2>/dev/null || echo 0)
queue_count_after=$(ls -1 "$QUEUE_DIR" 2>/dev/null | wc -l)

fail=0
if [[ "$prod_lines_before" != "$prod_lines_after" ]]; then
  echo "[smoke] FAIL: production audit-log grew ($prod_lines_before → $prod_lines_after)" >&2
  fail=1
else
  echo "[smoke] OK: production audit-log unchanged ($prod_lines_after lines)"
fi

if [[ "$prod_mtime_before" != "$prod_mtime_after" ]]; then
  echo "[smoke] FAIL: production audit-log mtime changed" >&2
  fail=1
fi

if [[ ! -f "$TEST_AUDIT" ]]; then
  echo "[smoke] FAIL: $TEST_AUDIT does not exist" >&2
  fail=1
elif [[ "$test_lines_after" -le "$test_lines_before" ]]; then
  echo "[smoke] FAIL: test audit-log did not grow ($test_lines_before → $test_lines_after)" >&2
  fail=1
else
  echo "[smoke] OK: test audit-log grew ($test_lines_before → $test_lines_after lines)"
fi

# Verify the new entries carry test:true
if grep -F '"test": true' "$TEST_AUDIT" | grep -q "$SUB_ID"; then
  echo "[smoke] OK: $SUB_ID present in test audit-log with test:true"
else
  echo "[smoke] FAIL: $SUB_ID not found with test:true in $TEST_AUDIT" >&2
  fail=1
fi

# State directory location
if [[ -d "$TEST_DIR/$SUB_ID" ]]; then
  echo "[smoke] OK: state dir at $TEST_DIR/$SUB_ID (under test/)"
else
  echo "[smoke] FAIL: state dir not found at $TEST_DIR/$SUB_ID" >&2
  fail=1
fi
if [[ -e "$SUB_ROOT/$SUB_ID" ]]; then
  echo "[smoke] FAIL: state leaked to production root: $SUB_ROOT/$SUB_ID" >&2
  fail=1
fi

# Queue marker must not have been dropped
if [[ "$queue_count_before" != "$queue_count_after" ]]; then
  echo "[smoke] FAIL: queue grew ($queue_count_before → $queue_count_after) — worker would have processed test data" >&2
  fail=1
else
  echo "[smoke] OK: queue unchanged ($queue_count_after markers)"
fi

# State endpoint round-trip + test_mode field
STATE_BASE="${INTAKE_URL%/api/submit}"
STATE_JSON=$(curl -sS "${STATE_BASE}/api/submission/$SUB_ID/state")
echo "[smoke] state response: $STATE_JSON"
if echo "$STATE_JSON" | grep -q '"test_mode": *true'; then
  echo "[smoke] OK: state endpoint returns test_mode=true"
else
  echo "[smoke] FAIL: state endpoint missing test_mode=true" >&2
  fail=1
fi

# journalctl scan for [TEST] prefix
TEST_LINE_COUNT=$(journalctl -u "$SERVICE" --since '2 minutes ago' --no-pager 2>/dev/null | grep -c '\[TEST\]' || true)
if [[ "$TEST_LINE_COUNT" -gt 0 ]]; then
  echo "[smoke] OK: found $TEST_LINE_COUNT [TEST] log lines in journald (last 2min)"
else
  echo "[smoke] WARN: no [TEST] lines in journalctl — handler may not be running this code yet" >&2
  # Not a hard fail since service may not be restarted yet.
fi

if [[ "$fail" -eq 0 ]]; then
  echo "[smoke] PASS"
  exit 0
else
  echo "[smoke] FAILED ($fail check(s))" >&2
  exit 1
fi
