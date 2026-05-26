#!/usr/bin/env bash
# Weekly sweep of stale ICSAC test-tier artifacts. Removes test state dirs,
# T2 outbox .eml files, and test review markdown older than RETENTION_DAYS.
# LEAVES audit-log-test.jsonl untouched (durable history) and queue/
# untouched (active state).
#
# Wired to cron on OPi3B: Sunday 04:00 ET.
# Pain nerve: failure curls /pain on OPi5 so a broken sweep doesn't go silent.
set -u
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TEST_ROOT="${HOME}/icsac-submissions/test"
TEST_OUTBOX="${TEST_ROOT}/_outbox"
TEST_REVIEWS="${HOME}/Desktop/icsac/editorial-system/reviews/test"
PAIN_URL="http://100.117.63.73:8090/pain"

deleted_dirs=0
deleted_eml=0
deleted_md=0

# Test state dirs (ICSAC-SUB-TEST-*) — oldest mtime older than RETENTION_DAYS.
if [ -d "$TEST_ROOT" ]; then
  while IFS= read -r d; do
    rm -rf "$d" && deleted_dirs=$((deleted_dirs + 1))
  done < <(find "$TEST_ROOT" -maxdepth 1 -type d -name 'ICSAC-SUB-TEST-*' -mtime +"$RETENTION_DAYS")
fi

# T2 outbox .eml files.
if [ -d "$TEST_OUTBOX" ]; then
  while IFS= read -r f; do
    rm -f "$f" && deleted_eml=$((deleted_eml + 1))
  done < <(find "$TEST_OUTBOX" -maxdepth 1 -type f -name '*.eml' -mtime +"$RETENTION_DAYS")
fi

# Test review markdown.
if [ -d "$TEST_REVIEWS" ]; then
  while IFS= read -r f; do
    rm -f "$f" && deleted_md=$((deleted_md + 1))
  done < <(find "$TEST_REVIEWS" -maxdepth 1 -type f -name 'ICSAC-SUB-TEST-*' -mtime +"$RETENTION_DAYS")
fi

echo "[$(date -Iseconds)] sweep-test-tiers: dirs=$deleted_dirs eml=$deleted_eml md=$deleted_md retention=${RETENTION_DAYS}d"
exit 0
