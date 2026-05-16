#!/usr/bin/env bash
# Smoke test for the Tier-3 (robust) pipeline. Wraps the tier-2 smoke
# test with TIER=3 so the same body, isolation checks, and ORCID
# redaction grep run; tier-3-specific verifications are appended below
# after the panel completes:
#   - Decision draft was attempted via IMAP (subject prefix `[T3 TEST] `)
#   - Zenodo sandbox call attempted (or graceful-skip log if token unset)
#   - Test Telegram call attempted (or graceful skip if env var unset)
#   - T3 invocation tripwire fired to operator chat at handoff (the
#     intake handler logs this line; we grep journald for it)
#
# Run with: bash scripts/smoke_test_tier_3.sh

set -euo pipefail

THIS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
TIER=3 bash "${THIS_DIR}/smoke_test_tier_2.sh"

# Above already validated the immediate isolation guarantees + waited
# for the panel. We do a follow-up pass on T3-specific artifacts.

SUB_ROOT="${SUB_ROOT:-$HOME/icsac-submissions}"
TEST_DIR="${SUB_ROOT}/test"
SERVICE="${SERVICE:-intake-server.service}"
WORKER_SERVICE="${WORKER_SERVICE:-submission-worker.service}"

# Most-recent test sub_id (tier-2 smoke only ever creates ICSAC-SUB-TEST-*).
LATEST_SUB_ID=$(ls -1t "$TEST_DIR" 2>/dev/null | grep '^ICSAC-SUB-TEST-' | head -n1 || true)
if [[ -z "$LATEST_SUB_ID" ]]; then
  echo "[t3] FAIL: no test sub dir found for follow-up checks" >&2
  exit 1
fi
echo "[t3] follow-up checks for $LATEST_SUB_ID"

# T3 tripwire — intake_server prints the warning via notify.send_telegram
# inline; we also wrote audit `tier_elevated` for tier 3. Grep journald.
INTAKE_LOG=$(journalctl -u "$SERVICE" --since '30 minutes ago' --no-pager 2>/dev/null || true)
if echo "$INTAKE_LOG" | grep -q 'T3 submission received'; then
  echo "[t3] OK: T3 submission received line in journald"
else
  echo "[t3] WARN: T3 submission received line not seen in journald (service may not be restarted yet)"
fi

# Worker journald: look for sandbox handling + draft delivery.
WORKER_LOG=$(journalctl -u "$WORKER_SERVICE" --since '30 minutes ago' --no-pager 2>/dev/null || true)

if echo "$WORKER_LOG" | grep -q "deposit-draft: SKIPPED -- ZENODO_SANDBOX_TOKEN" \
   || echo "$WORKER_LOG" | grep -q "deposit-draft: SKIPPED — ZENODO_SANDBOX_TOKEN"; then
  echo "[t3] OK: Zenodo sandbox gracefully skipped (token unset, as designed)"
elif echo "$WORKER_LOG" | grep -q "deposit-draft: staged record_id="; then
  echo "[t3] OK: Zenodo sandbox deposit attempted and succeeded"
else
  # Decision may have been "decline" so deposit was skipped by design;
  # only flag this when verdict was accept.
  echo "[t3] note: no Zenodo deposit log line — verdict may have been decline (deposit-only-on-accept)"
fi

if echo "$WORKER_LOG" | grep -F "TELEGRAM_TEST_CHAT_ID not set" >/dev/null; then
  echo "[t3] OK: test Telegram chat gracefully skipped (env var unset, as designed)"
elif echo "$WORKER_LOG" | grep -F "[T3 TEST] ICSAC submission complete" >/dev/null; then
  echo "[t3] OK: test Telegram chat ping fired"
else
  echo "[t3] note: no Telegram chat log seen for completion (panel may not be done)"
fi

# IMAP draft attempt — the `_email_decision` path for tier=3 invokes
# notify_author with draft=True + `[T3 TEST] ` subject prefix. We can't
# inspect Gmail from here, but we can scan journald for the attempt.
if echo "$WORKER_LOG" | grep -F "draft saved to [Gmail]/Drafts" >/dev/null; then
  echo "[t3] OK: IMAP draft save reported"
elif echo "$WORKER_LOG" | grep -F "IMAP error" >/dev/null; then
  echo "[t3] WARN: IMAP error logged — check Gmail app password / IMAP enabled"
else
  echo "[t3] note: no IMAP draft log line yet (panel may still be running, or verdict was REVIEW_FURTHER which skips email)"
fi

echo "[t3] follow-up checks complete"
