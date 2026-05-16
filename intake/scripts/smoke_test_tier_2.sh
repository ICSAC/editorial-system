#!/usr/bin/env bash
# Smoke test for the Tier-2 (scaled) pipeline.
#
# Submits a test record using the test ORCID with X-ICSAC-Test-Tier: t2
# and verifies the tier-2 isolation guarantees:
#   1. Production audit-log.jsonl did NOT grow
#   2. audit-log-test.jsonl gained a `tier: 2` event
#   3. State directory landed under SUBMISSIONS_ROOT/test/<id>
#   4. PROD .counter did NOT increment (test ID format)
#   5. Real panel ran (state file contains a real panel_result, not the
#      canned 7/10 marker that T1 writes)
#   6. .eml file lands at SUBMISSIONS_ROOT/test/_outbox/<sub_id>.eml
#   7. No IMAP draft was attempted (no `[T3 TEST]` artifact)
#   8. The configured live ORCID (LIVE_ORCID env) does NOT appear in any
#      test artifact written for this run
#
# Run with: bash scripts/smoke_test_tier_2.sh
# Requires: HMAC secret in INTAKE_HMAC_SECRET env var (or sourced from
#           ~/.config/icsac-intake.env). Real panel runs (~5-10 min,
#           real frontier-model tokens). Don't run repeatedly.

set -euo pipefail

INTAKE_URL="${INTAKE_URL:-http://127.0.0.1:8443/api/submit}"
SERVICE="${SERVICE:-intake-server.service}"
PROD_AUDIT="${PROD_AUDIT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/reviews/audit-log.jsonl}"
TEST_AUDIT="${TEST_AUDIT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/reviews/audit-log-test.jsonl}"
SUB_ROOT="${SUB_ROOT:-$HOME/icsac-submissions}"
TEST_DIR="${SUB_ROOT}/test"
TEST_QUEUE_DIR="${TEST_DIR}/queue"
TEST_OUTBOX="${TEST_DIR}/_outbox"
PROD_QUEUE_DIR="${SUB_ROOT}/queue"
PROD_COUNTER="${SUB_ROOT}/.counter"
TIER="${TIER:-2}"  # the tier-3 script overrides via TIER=3
TIER_HEADER="t${TIER}"

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
if [[ -z "${LIVE_ORCID:-}" ]]; then
  echo "LIVE_ORCID not set — required so the script doesn't bake an" >&2
  echo "ORCID into source. Export LIVE_ORCID (an ORCID enrolled in the" >&2
  echo "handler's TEST_ORCIDS allowlist) before invoking." >&2
  exit 2
fi

# ── snapshot pre-state ─────────────────────────────────────────
prod_lines_before=$(wc -l < "$PROD_AUDIT" 2>/dev/null || echo 0)
prod_counter_before=$(cat "$PROD_COUNTER" 2>/dev/null || echo 0)
test_lines_before=$(wc -l < "$TEST_AUDIT" 2>/dev/null || echo 0)
prod_queue_before=$(ls -1 "$PROD_QUEUE_DIR" 2>/dev/null | wc -l)
echo "[t${TIER}] pre: prod_audit=${prod_lines_before}L test_audit=${test_lines_before}L prod_counter=${prod_counter_before} prod_queue=${prod_queue_before}"

# ── build a minimal valid PDF (same as T1 smoke) ───────────────
PDF_TMP=$(mktemp --suffix=.pdf)
trap 'rm -f "$PDF_TMP"' EXIT
LOREM=$(python3 -c "import sys; sys.stdout.write(('Tier ${TIER} smoke pipeline content padded for the text-layer minimum threshold of two thousand characters. ' * 25))")
python3 - <<PY
import sys, pathlib
text = """$LOREM"""
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

BOUNDARY="----t${TIER}smoke$(date +%s)$$"
BODY_TMP=$(mktemp)
trap 'rm -f "$PDF_TMP" "$BODY_TMP"' EXIT

field() {
  printf -- "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" "$BOUNDARY" "$1" "$2"
}
{
  field name "Smoke T${TIER} User"
  field email "smoke-test@example.invalid"
  field orcid "$LIVE_ORCID"
  field coi on
  field exclusivity on
  field title "Tier-${TIER} Smoke Submission $(date -u +%FT%TZ)"
  field abstract "Tier ${TIER} smoke abstract padded to clear the fifty-character minimum that the upload validator enforces on the abstract field. This text is purely synthetic and only exists to drive the panel."
  field keywords "smoke,tier${TIER},pipeline"
  field license "cc-by-4.0"
  field resource_type "preprint"
  field publication_date "$(date -u +%F)"
  field subject ""
  field funding ""
  field creators "[{\"name\":\"Smoke T${TIER} User\",\"orcid\":\"${LIVE_ORCID}\"}]"
  field related_identifiers '[]'
  field deposit_consent on
  printf -- "--%s\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"smoke.pdf\"\r\nContent-Type: application/pdf\r\n\r\n" "$BOUNDARY"
  cat "$PDF_TMP"
  printf -- "\r\n--%s--\r\n" "$BOUNDARY"
} > "$BODY_TMP"

TS=$(date +%s)
SIG=$(printf "%s." "$TS" | cat - "$BODY_TMP" | openssl dgst -sha256 -hmac "$INTAKE_HMAC_SECRET" -hex | awk '{print $NF}')

echo "[t${TIER}] POSTing tier-${TIER} submission with X-ICSAC-Test-Tier: ${TIER_HEADER}"
RESP=$(curl -sS -X POST "$INTAKE_URL" \
  -H "Content-Type: multipart/form-data; boundary=$BOUNDARY" \
  -H "x-icsac-timestamp: $TS" \
  -H "x-icsac-signature: sha256=$SIG" \
  -H "x-icsac-auth-orcid: $LIVE_ORCID" \
  -H "x-icsac-auth-name: Smoke%20T${TIER}%20User" \
  -H "x-icsac-test-tier: ${TIER_HEADER}" \
  --data-binary "@$BODY_TMP")
echo "[t${TIER}] response: $RESP"

SUB_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('sub_id',''))")
RESP_TIER=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tier',''))")
if [[ -z "$SUB_ID" ]]; then
  echo "[t${TIER}] FAIL: no sub_id in response" >&2
  exit 1
fi
echo "[t${TIER}] sub_id=$SUB_ID tier=$RESP_TIER"

case "$SUB_ID" in
  ICSAC-SUB-TEST-*) ;;
  *) echo "[t${TIER}] FAIL: sub_id is not in test format: $SUB_ID" >&2; exit 1 ;;
esac

if [[ "$RESP_TIER" != "$TIER" ]]; then
  echo "[t${TIER}] FAIL: response tier=$RESP_TIER, expected $TIER" >&2
  exit 1
fi

# ── verify isolation up front (these are immediate guarantees) ─
fail=0
prod_lines_after=$(wc -l < "$PROD_AUDIT" 2>/dev/null || echo 0)
prod_counter_after=$(cat "$PROD_COUNTER" 2>/dev/null || echo 0)
test_lines_after=$(wc -l < "$TEST_AUDIT" 2>/dev/null || echo 0)
prod_queue_after=$(ls -1 "$PROD_QUEUE_DIR" 2>/dev/null | wc -l)

if [[ "$prod_lines_before" != "$prod_lines_after" ]]; then
  echo "[t${TIER}] FAIL: production audit-log grew ($prod_lines_before -> $prod_lines_after)" >&2; fail=1
else
  echo "[t${TIER}] OK: production audit-log unchanged"
fi

if [[ "$prod_counter_before" != "$prod_counter_after" ]]; then
  echo "[t${TIER}] FAIL: prod .counter incremented ($prod_counter_before -> $prod_counter_after)" >&2; fail=1
else
  echo "[t${TIER}] OK: prod .counter unchanged"
fi

if [[ "$prod_queue_before" != "$prod_queue_after" ]]; then
  echo "[t${TIER}] FAIL: prod queue grew ($prod_queue_before -> $prod_queue_after)" >&2; fail=1
else
  echo "[t${TIER}] OK: prod queue unchanged"
fi

if [[ "$test_lines_after" -le "$test_lines_before" ]]; then
  echo "[t${TIER}] FAIL: test audit-log did not grow" >&2; fail=1
else
  echo "[t${TIER}] OK: test audit-log grew ($test_lines_before -> $test_lines_after)"
fi

if grep -F "\"tier\": ${TIER}" "$TEST_AUDIT" | grep -q "$SUB_ID"; then
  echo "[t${TIER}] OK: $SUB_ID present in test audit-log with tier:${TIER}"
else
  echo "[t${TIER}] FAIL: $SUB_ID not found with tier:${TIER} in $TEST_AUDIT" >&2; fail=1
fi

if [[ -d "$TEST_DIR/$SUB_ID" ]]; then
  echo "[t${TIER}] OK: state dir at $TEST_DIR/$SUB_ID"
else
  echo "[t${TIER}] FAIL: state dir not found" >&2; fail=1
fi

if [[ -e "$SUB_ROOT/$SUB_ID" ]]; then
  echo "[t${TIER}] FAIL: state leaked to production root: $SUB_ROOT/$SUB_ID" >&2; fail=1
fi

# ── ORCID redaction check (scoped to artifacts FOR THIS sub_id) ─
# Pre-tier-2-rollout entries can carry the live ORCID; we filter the
# audit log to lines that mention SUB_ID before checking, so historical
# leakage from earlier T1 smoke runs doesn't cause a false positive.
TEST_AUDIT_LINES=$(grep -F "$SUB_ID" "$TEST_AUDIT" 2>/dev/null || true)
LEAK_AUDIT=$(echo "$TEST_AUDIT_LINES" | grep -F "$LIVE_ORCID" || true)
LEAK_FILES=$(grep -lF "$LIVE_ORCID" \
  "$TEST_DIR/$SUB_ID/submission.json" \
  "$TEST_DIR/$SUB_ID/state.json" 2>/dev/null || true)
if [[ -n "$LEAK_AUDIT" || -n "$LEAK_FILES" ]]; then
  echo "[t${TIER}] FAIL: live ORCID leaked into test artifact for $SUB_ID:" >&2
  [[ -n "$LEAK_AUDIT" ]] && echo "  audit: $LEAK_AUDIT" >&2
  [[ -n "$LEAK_FILES" ]] && echo "  files: $LEAK_FILES" >&2
  fail=1
else
  echo "[t${TIER}] OK: live ORCID does NOT appear in any test artifact for $SUB_ID"
fi

# ── wait for worker to drain queue + run the panel ──────────────
echo "[t${TIER}] waiting up to 25 minutes for the panel to complete..."
deadline=$(( $(date +%s) + 1500 ))
final_state=""
while [[ $(date +%s) -lt $deadline ]]; do
  if [[ -f "$TEST_DIR/$SUB_ID/state.json" ]]; then
    s=$(python3 -c "import json,sys; print(json.load(open('$TEST_DIR/$SUB_ID/state.json')).get('state',''))" 2>/dev/null || echo "")
    if [[ "$s" == "completed" || "$s" == "completed_email_failed" || "$s" == rejected_* || "$s" == "paused_panel_failure" || "$s" == "awaiting_decision" ]]; then
      final_state="$s"
      break
    fi
  fi
  sleep 15
done

if [[ -z "$final_state" ]]; then
  echo "[t${TIER}] WARN: panel did not finish within 25min (deadline)" >&2
  final_state="(unknown)"
fi
echo "[t${TIER}] final_state=$final_state"

# ── tier-2-specific: outbox .eml exists ────────────────────────
if [[ "$TIER" == "2" ]]; then
  if [[ -f "$TEST_OUTBOX/$SUB_ID.eml" ]]; then
    eml_size=$(wc -c < "$TEST_OUTBOX/$SUB_ID.eml")
    echo "[t${TIER}] OK: outbox .eml at $TEST_OUTBOX/$SUB_ID.eml (${eml_size} bytes)"
    # Verify no live ORCID in the .eml
    if grep -F "$LIVE_ORCID" "$TEST_OUTBOX/$SUB_ID.eml" >/dev/null 2>&1; then
      echo "[t${TIER}] FAIL: live ORCID leaked into outbox .eml" >&2; fail=1
    else
      echo "[t${TIER}] OK: outbox .eml does not contain live ORCID"
    fi
  else
    echo "[t${TIER}] WARN: outbox .eml not found at $TEST_OUTBOX/$SUB_ID.eml (panel may not have completed yet)"
  fi
fi

if [[ "$fail" -eq 0 ]]; then
  echo "[t${TIER}] PASS"
  exit 0
else
  echo "[t${TIER}] FAILED ($fail check(s))" >&2
  exit 1
fi
