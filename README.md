---
title: ICSAC Editorial System
emoji: 📄
colorFrom: indigo
colorTo: gray
sdk: static
pinned: false
license: mit
short_description: Open-source AI editorial pipeline for ICSAC submissions
---

# ICSAC Editorial System

This is the open-source AI editorial pipeline that accepts and reviews every
submission to the [Institute for Complexity Science and Advanced Computing](https://icsacinstitute.org)
and its journal [*Persistence*](https://icsacinstitute.org/journal) ([`/submit`](https://icsacinstitute.org/submit)).

If you sent a paper to ICSAC — this repository is exactly what received and
read it. The submission handler, the rubrics, the prompts, the citation checks,
the redaction layer, the audit pass that scores the panel itself. With one
defensive exception (the prompt-injection signal in the RQC audit is not
published, to avoid teaching adversarial submissions what to avoid — see
[`rubrics/review_quality_control.md`](rubrics/review_quality_control.md) for
the rationale), nothing about how your work is evaluated is hidden.

## Why this is public

Independent and heterodox researchers have legitimate cause to be skeptical of
black-box editorial AI. The reasonable response is not to hide the system
behind a "trust us." The reasonable response is to publish it.

You can read the rubrics ICSAC reviewers apply ([`rubrics/`](rubrics/)). You can
read the templates that draft the acceptance and revise-and-resubmit letters
([`templates/`](templates/), [`intake/templates/`](intake/templates/)). You can
read the redacted reviews of every accepted paper at
[icsacinstitute.org/accepted](https://icsacinstitute.org/accepted) — produced
by the exact redaction layer in this repo. You can run the pipeline yourself.

## What this repo does NOT include

This repo is the AI reviewer half of the institute. **Intentionally not public**:
the manuscript archive, author records, member directory, internal moderation
tooling, the website that fronts all of this, and the operational infrastructure
that runs it. Those remain private to protect the privacy and security of
authors, members, the team, affiliates, and site visitors. What this repository
exposes is *how* your manuscript was evaluated and *what* was recorded — not
the people, payment, identity, or infrastructure layers around that
evaluation.

## What it does

The pipeline has two halves: a long-running **submission front-end** that
accepts new manuscripts, and a **batch review pipeline** that runs on a timer
and drains the queue. Both live in this repo.

For each submission:

1. **Intake** ([`intake/intake_server.py`](intake/intake_server.py)) — the
   FastAPI handler behind `icsacinstitute.org/submit` accepts a DOI / arXiv
   reference OR a direct PDF upload. The upstream form host verifies the
   submitter's ORCID via OAuth and signs each request with an HMAC; this
   handler re-validates and persists the submission record.
2. **Manuscript fetch** ([`submission_intake.py`](submission_intake.py)) —
   the worker either downloads the manuscript from the resolver (DOI route)
   or uses the uploaded PDF as the archive of record. Text and references are
   extracted.
3. **Citation verification** ([`citation_verify.py`](citation_verify.py),
   [`citation_misattribution.py`](citation_misattribution.py)) — every cited
   work is checked against arXiv, Crossref, and Semantic Scholar. Fabricated
   and misattributed citations are flagged.
4. **Five-reviewer panel** ([`review.py`](review.py)) — a panel of independent
   model instances reviews the manuscript against ICSAC's rubrics (scope,
   methodology, calibration, tone, AI provenance signal). Each reviewer scores
   blind to the others. The panel tolerates one slot failure per pass via
   self-heal (MIN_REVIEWERS=4), so a published review may reflect four valid
   outputs if a slot errored.
5. **Review quality control** ([`review_quality_control.py`](review_quality_control.py)) —
   a separate auditor reviews the panel itself. Low-confidence dimensions,
   missing injection indicators, or systemic drift trigger curator alerts.
6. **Redaction** ([`redaction.py`](redaction.py)) — internal reasoning, vendor
   names, and operational metadata are stripped before any review is shared
   with the author or published.
7. **Decision** ([`action.py`](action.py)) — the panel recommends one of three
   outcomes:
   - **Accept** — published to `icsacinstitute.org/accepted/<id>` with a
     redacted copy of the panel's review.
   - **Revise and resubmit** — author receives the panel's feedback and may
     resubmit a revised version.
   - **Scope reject** — reserved for submissions that fall outside the
     institute's remit (pseudoscience, non-engageable). Not a standard
     editorial outcome.

   Every panel run produces a *recommendation*, not a verdict. The
   recommendation, the full panel report, and the RQC audit are sent to
   the curator via the operator's configured notification channel. The
   curator confirms, modifies, or overrides before any author email is
   drafted, any publications-registry entry is written, or any Zenodo
   deposit is staged. The panel never auto-delivers a decision.

## What it is not

This is the system one institute uses to evaluate its own submissions. It is
not a general-purpose academic peer review platform. It is not a service you
can submit to without going through `icsacinstitute.org/submit`. It is not a
replacement for human editorial judgment — every panel outcome routes to a
human curator for the final verdict, and any author who disagrees with that
verdict can appeal to a human editor.

It is also opinionated. The rubrics reflect the institute's editorial scope:
complexity science, information theory, persistence dynamics, and adjacent
methodology. A submission outside that scope will be flagged as out-of-scope
regardless of its merit on its own terms.

## Repository layout

The editorial-side modules live at the top level. The submission front-end is
a Python subpackage at [`intake/`](intake/).

### Editorial pipeline (top level)

| Path | Purpose |
|------|---------|
| `editorial_workflow.py` | Top-level workflow orchestration |
| `submission_intake.py` | Manuscript fetching and text extraction |
| `citation_verify.py` | Cross-reference citation validation |
| `citation_misattribution.py` | Catches "real DOI, wrong paper" errors |
| `review.py` | The five-reviewer panel |
| `review_quality_control.py` | Auditor that scores the panel |
| `redaction.py` | Redaction layer for public review output |
| `action.py` | Decision dispatch (accept / R&R / scope reject) |
| `repository_deposit.py` | Stages accepted papers to a repository backend (Zenodo) |
| `email_send.py`, `email_render.py` | Author correspondence primitives |
| `publications.py`, `publish_watcher.py` | Post-acceptance publication |
| `watch.py` | Polls Zenodo for curator state transitions and posts branded comments on accept/decline |
| `notify.py` | Curator-channel notification helpers (channel-agnostic dispatcher; forkable) |
| `directory.py` | Community directory data layer |
| `stats.py` | Aggregate statistics over historical reviews |
| `rubrics/` | The editorial rubrics applied to every submission |
| `templates/` | Editorial-side correspondence templates (Zenodo-comment posts, etc.) |
| `editorial-batch.{service,timer}`, `editorial-review.{service,timer}`, `submission-watcher.{service,timer}` | systemd units for the batch workflow, community-request poller, and intake/decision watcher |

### Submission front-end ([`intake/`](intake/))

| Path | Purpose |
|------|---------|
| `intake/intake_server.py` | FastAPI app: `POST /api/submit`, `GET /api/submission/{id}/state`, `GET /healthz` |
| `intake/submission_worker.py` | Drains the queue, resolves deferred DOIs, dispatches into the review pipeline |
| `intake/apply_decision.py` | Applies the curator's verdict: publications registration, deposit staging, author email |
| `intake/notify_author.py` | Author email rendering; attaches redacted panel report + RQC as PDFs |
| `intake/rehydrate.py` | Refetch a stubbed DOI submission's bytes from the resolver, verify SHA |
| `intake/templates/` | Author-facing email templates |
| `intake/scripts/` | Smoke tests for the test-mode pipeline |
| `intake/README.md` | Subpackage-scoped documentation |

## Running it yourself

The pipeline is designed to run on a single host. The submission server runs
as a long-lived FastAPI service; the editorial workflow runs from systemd
timers (twice daily by default) and drains the queue when it ticks.

Dependencies:

- A submission repository (we use Zenodo — needs an API token with read + deposit scopes)
- An LLM provider (OpenRouter, Anthropic, or compatible)
- An SMTP account for author correspondence
- A registry destination (the institute's website repository, in our case) for
  post-acceptance publication of accepted papers and redacted reviews

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py     # edit for SMTP, Zenodo, LLM provider
export INTAKE_HMAC_SECRET=$(openssl rand -hex 32)   # for the intake HMAC gate

# Submission server (long-running):
uvicorn intake.intake_server:app --host 127.0.0.1 --port 8443 --log-level warning

# Submission worker (drains the queue when markers appear):
python -m intake.submission_worker

# Editorial batch tick (run from systemd timer in production):
python editorial_workflow.py batch-tick
```

Set secrets in `/etc/icsac/editorial.env` (loaded by systemd `EnvironmentFile=`).
For manual runs, source the same file. See [`intake/README.md`](intake/README.md)
for the full list of intake-side environment variables.

## License

MIT. See [`LICENSE`](LICENSE).

The rubrics and review templates are MIT-licensed code artifacts — feel free
to fork, adapt, and use as the basis for your own institute's review system.

## A note to authors

If your paper was reviewed by this system and you disagree with the panel's
recommendation: write to `help@icsacinstitute.org`. A human editor reads every
appeal. The panel is not the last word — it is a thorough first pass that the
curator turns into the verdict, and that the editor overrides on appeal.

If your paper was accepted: the redacted review is published at
`icsacinstitute.org/accepted/<your-record-id>` alongside the work itself.

If you want to know exactly which prompts the panel saw, which rubrics it
applied, which citations it verified, and which form fields the submission
handler collected before forming its recommendation: read the source. That is
why this repository exists.
