#!/bin/bash
# Hourly watcher (cron) for the Crossref TEST system after a password change.
# test.crossref.org syncs credentials from production on its own schedule; until it
# does, intake/register-doi.sh <id> (TEST) answers 401 and --live must wait. This
# script pings the curation team ONCE when the test login turns green, or ONCE if it
# is still red after 48 h, then goes quiet. State lives in ~/.cache/icsac/.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CACHE="$HOME/.cache/icsac"; mkdir -p "$CACHE"
DONE="$CACHE/crossref-test-probe.done"; STARTED="$CACHE/crossref-test-probe.started"
[ -f "$DONE" ] && exit 0
[ -f "$STARTED" ] || date -u +%s > "$STARTED"
set -a; . "$HOME/.config/crossref.env"; . "$HOME/.config/zenodo-pipeline.env"; set +a
ping() { (cd "$REPO" && .venv/bin/python -c "import sys, notify; notify.send_to_curator(sys.argv[1], parse_mode=None)" "$1"); }
# The credential rides in a curl config on stdin, never in argv (audit 2026-09-28 pass B).
body=$(printf 'url = "https://test.crossref.org/servlet/login?usr=%s/%s&pwd=%s"\n' \
         "${CROSSREF_LOGIN_EMAIL}" "${CROSSREF_ROLE}" "${CROSSREF_PASSWORD}" \
       | curl -s -m 30 -w '\n%{http_code}' -K -)
code=$(echo "$body" | tail -1)
if [ "$code" = "200" ] && ! echo "$body" | grep -qi "wrong credentials"; then
  echo "$(date -u +%FT%TZ) GREEN"
  ping "Crossref TEST now accepts the password (HTTP 200). Next: cd ~/Desktop/icsac/editorial-system && intake/register-doi.sh <sub-id>   (TEST deposit, nothing public). If that reads Success, --live is yours when you decide."
  date -u +%FT%TZ > "$DONE"; exit 0
fi
echo "$(date -u +%FT%TZ) still $code"
age=$(( $(date -u +%s) - $(cat "$STARTED") ))
if [ "$age" -ge $((48*3600)) ]; then
  ping "Crossref TEST still rejects the password 48 h after the reset (HTTP $code). Production accepts it. Ask support@crossref.org to sync test.crossref.org for help@icsacinstitute.org, or decide whether to skip the TEST deposit. This watcher is now quiet."
  date -u +%FT%TZ > "$DONE"
fi
exit 0
