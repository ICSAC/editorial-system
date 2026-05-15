# ICSAC Editorial System

This is the open-source AI editorial system that reviews every submission to the
[Institute for Complexity Science and Advanced Computing](https://icsacinstitute.org)
([`/submit`](https://icsacinstitute.org/submit)).

If you sent a paper to ICSAC — this repository is exactly what read it. The
rubrics, the prompts, the citation checks, the redaction layer, the audit
pass that scores the panel itself. Nothing about how your work is evaluated is
hidden.

## Why this is public

Independent and heterodox researchers have legitimate cause to be skeptical of
black-box editorial AI. The reasonable response is not to hide the system
behind a "trust us." The reasonable response is to publish it.

You can read the rubrics ICSAC reviewers apply ([`rubrics/`](rubrics/)). You can
read the templates that draft the acceptance and revise-and-resubmit letters
([`templates/`](templates/)). You can read worked examples of the system
reviewing real accepted papers ([`reviews/`](reviews/)). You can run it
yourself.

## What it does

For each submission (DOI to a Zenodo preprint, or direct PDF upload):

1. **Intake** — fetches the manuscript, extracts text and references.
2. **Citation verification** — every cited work is checked against arXiv,
   Crossref, and ADS. Fabricated and misattributed citations are flagged.
3. **Five-reviewer panel** — a panel of independent model instances reviews
   the manuscript against ICSAC's rubrics (scope, methodology, calibration,
   tone, slop detection). Each reviewer scores blind to the others.
4. **Review quality control** — a separate auditor reviews the panel itself.
   Low-confidence dimensions, missing injection indicators, or systemic drift
   trigger operator alerts.
5. **Redaction** — internal reasoning, vendor names, and operational metadata
   are stripped before any review is shared with the author or published.
6. **Decision** — the panel recommends one of three outcomes:
   - **Accept** — published to `icsacinstitute.org/accepted/<id>` with a
     scrubbed copy of the panel's review.
   - **Revise and resubmit** — author receives the panel's feedback and may
     resubmit a revised version.
   - **Reject** — reserved for submissions that fall outside the institute's
     remit (pseudoscience, non-engageable). Not a standard editorial outcome.

## What it is not

This is the system one institute uses to evaluate its own submissions. It is
not a general-purpose academic peer review platform. It is not a service you
can submit to without going through `icsacinstitute.org/submit`. It is not a
replacement for human editorial judgment — the panel's recommendation is the
last step before a human editor accepts or declines.

It is also opinionated. The rubrics reflect the institute's editorial scope:
complexity science, information theory, persistence dynamics, and adjacent
methodology. A submission outside that scope will be flagged as out-of-scope
regardless of its merit on its own terms.

## Repository layout

| Path | Purpose |
|------|---------|
| `pipeline.py` | Top-level workflow orchestration |
| `ingest.py` | Manuscript fetching and text extraction |
| `citation_verify.py` | Cross-reference citation validation |
| `citation_misattribution.py` | Catches "real DOI, wrong paper" errors |
| `review.py` | The five-reviewer panel |
| `review_quality_control.py` | Auditor that scores the panel |
| `scrubber.py` | Redaction layer for public review output |
| `action.py` | Decision dispatch (accept / R&R / reject) |
| `email_send.py`, `email_render.py` | Author correspondence |
| `publications.py`, `publish_watcher.py` | Post-acceptance publication |
| `rubrics/` | The editorial rubrics applied to every submission |
| `templates/` | Author-facing correspondence templates |
| `reviews/` | Worked examples — real reviews of accepted ICSAC papers |
| `*.service`, `*.timer` | systemd units for the batch and watcher daemons |

## Running it yourself

The system is designed to run as a long-lived service on a single host, polling
a configured Zenodo community for new submissions and dispatching them through
the panel. It depends on:

- A Zenodo API token (read + deposit scopes)
- An LLM provider (OpenRouter, Anthropic, or compatible)
- An SMTP account for author correspondence
- A registry destination (the institute's website repository, in our case) for
  post-acceptance publication of accepted papers and scrubbed reviews

Copy `config.example.py` to `config.py` and fill in the relevant values.
Environment variables override config defaults; see `config.example.py` for
the full list.

## License

MIT. See [`LICENSE`](LICENSE).

The rubrics and review templates are MIT-licensed code artifacts — feel free
to fork, adapt, and use as the basis for your own institute's review system.
The reviews in `reviews/` are published by their authors under the
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) license that governs
all accepted ICSAC submissions.

## A note to authors

If your paper was reviewed by this system and you disagree with the panel's
recommendation: write to `info@icsacinstitute.org`. A human editor reads every
appeal. The panel is not the last word — it is a thorough first pass.

If your paper was accepted: the scrubbed review is published at
`icsacinstitute.org/accepted/<your-record-id>` alongside the work itself.

If you want to know exactly which prompts the panel saw, which rubrics it
applied, and which citations it verified before forming its recommendation:
read the source. That is why this repository exists.
