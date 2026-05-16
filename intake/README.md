# `intake/` — submission front-end

This subpackage is the HTTP submission server for the editorial pipeline.
See the top-level [README](../README.md) for the full architecture.

The intake handler lives behind [`icsacinstitute.org/submit`](https://icsacinstitute.org/submit).
It authenticates the upstream form, accepts a DOI / arXiv reference OR a
direct PDF upload, persists the submission record, and drops a queue
marker for the editorial worker to pick up on its next batch tick.

## What lives here

| File | Role |
|------|------|
| `intake_server.py` | FastAPI app: `POST /api/submit`, `GET /api/submission/{id}/state`, `GET /healthz`. |
| `submission_worker.py` | Drains the queue, resolves deferred DOIs, calls the editorial review pipeline (`review.review_paper`), routes verdicts. |
| `apply_decision.py` | Finalizes a curator-driven decision on borderline submissions. |
| `notify_author.py` | Author-facing email. Renders panel report + RQC to PDF (WeasyPrint) and attaches. Exposes `send_published()` — imported by editorial's `publish_watcher`. |
| `rehydrate.py` / `rehydrate.sh` | Refetch a stubbed DOI submission's bytes from the resolver, verify SHA. |
| `decide.sh` | Wrapper around `apply_decision.py` for invocation from a remote curator channel. |
| `time_fmt.py` | UTC-on-disk / ET-on-display timestamp helpers. |
| `templates/` | Author-facing email templates (Markdown with `{{var}}` interpolation). |
| `scripts/` | Smoke tests for the test-mode pipeline. |

## Required environment

| Variable | Required | Purpose |
|----------|----------|---------|
| `INTAKE_HMAC_SECRET` | **yes** | Shared secret with the upstream form host. Used to verify `X-ICSAC-Signature`. |
| `MAX_PDF_BYTES` | no | Upload size cap (default 25 MB). |
| `TEST_ORCIDS` | no | Comma-separated ORCIDs that short-circuit to the test pipeline. Empty default (test mode off). |
| `TEST_TIERS_DISABLED` | no | Force tier-1 (canned) for all test ORCIDs even if a header requests T2/T3. |
| `INCIDENT_SSH_HOST` | no | `user@host` for a remote incident-router (borderline-submission escalation). Empty default = skip remote handoff. |
| `INCIDENT_REMOTE_DIR` | no | Directory on `INCIDENT_SSH_HOST` to write incident JSON. |
| `ALERT_WEBHOOK_URL` | no | POST endpoint for operator-channel alerts (e.g., `ntfy`). Empty default = silent. |
| `INTAKE_NODE_NAME` | no | Display name prefixed on alert titles. Default `intake`. |
| `TELEGRAM_TEST_CHAT_ID` | no | Chat ID for test-tier Telegram drafts. Empty default = skip. |

Editorial-pipeline-side environment (SMTP, Zenodo token, etc.) is documented
in `../config.example.py`. Intake imports from the parent package directly;
there is no separate sibling-import path to configure.

## Running locally

From the repo root (not from `intake/`):

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.py config.py     # edit as needed for the editorial side

export INTAKE_HMAC_SECRET=$(openssl rand -hex 32)

# Submission server (terminal 1):
uvicorn intake.intake_server:app --host 127.0.0.1 --port 8443 --log-level warning

# Submission worker (terminal 2 — drains queue when markers appear):
python -m intake.submission_worker
```

The handler binds to localhost by default. In our deployment it sits behind a
reverse proxy; for a public deployment, put your own proxy in front (the
handler trusts the HMAC, not the network).

## Import contract

`publish_watcher.py` at the top level imports `send_published()` from this
subpackage:

```python
from intake.notify_author import send_published
```

The filename `notify_author.py` and the `send_published` signature are the
internal contract between the two halves. The other helpers in
`notify_author.py` are private.

## A note to authors

Every field on the form lands here. `submission.json` records: name, email,
ORCID, license choice, deposit consent (upload route), and the manuscript
reference. No other fields are collected. The PDF (for upload submissions)
is kept as the archive of record. For DOI submissions, the manuscript bytes
are lazy-rehydratable from the resolver, and only a SHA-anchored stub is
kept locally after review completes.
