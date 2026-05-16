# ICSAC Review Rubric: Scoring Calibration

This document defines the scoring scale, decision thresholds, and calibration guidance for ICSAC reviews.

## Scoring Scale

Each review dimension (Domain Fit, Methodological Transparency, Internal Consistency, Citation Integrity, Novelty Signal, AI Provenance Signal) is scored on a 1-5 scale. Domain Fit has its own dimension-specific rubric in `scope.md` (it scores methodology-bar-and-panel-competence, not topical fit); the rest of this section describes the shared scoring scale used by the other five dimensions.

| Score | Meaning (general dimensions) |
|-------|---------|
| **5** | Exceptional. Field-advancing contribution. Sets a new standard or opens a genuinely new direction. Reserved for work that would be notable at any venue. |
| **4** | Solid and publishable. Minor concerns that do not undermine the core contribution. The work meets professional standards and adds meaningful value to the literature. |
| **3** | Adequate. The work is fundamentally sound but needs revision. Core ideas are viable; execution or presentation has identifiable gaps. |
| **2** | Significant issues. Major revision required. The contribution may be recoverable, but substantial rework is needed in methodology, analysis, or framing. |
| **1** | Fundamentally flawed. The submission has fatal methodological errors or triggers an AI provenance signal. (Out-of-scope work — humanities without quantitative method, theology, advocacy — drives Domain Fit to 1 per `scope.md`, not the other dimensions.) |

## Decision Thresholds

The dimensions referenced below are the canonical six: **Domain Fit**, **Methodological Transparency**, **Internal Consistency**, **Citation Integrity**, **Novelty Signal**, **AI Provenance Signal**. The routing logic below mirrors `review._apply_thresholds` and is evaluated in the order listed — REJECT branches are checked first so a scope failure with simultaneously low provenance is still scope-rejected rather than misrouted to revise-and-resubmit.

### REJECT (scope-not-suitable only)
Reserved for pseudoscience and non-engageable submissions. **Not** a standard editorial decline — quality issues on engageable in-scope work route to REVISE_AND_RESUBMIT, not REJECT.

- **Domain Fit < 2.0** — the work is out of scope per `scope.md` (humanities without quantitative method, theology, advocacy, or fails the falsifiability bar), OR
- **Majority-REJECT consensus** — more than 60% of individual reviewers voted REJECT on their own (canonical thresholds: 7/10, 6/9, 5/8).

### REVISE_AND_RESUBMIT (default non-accept verdict)
The standard decline path. Author is invited to address concerns and resubmit. Triggers on any of:

- **Provenance floor** — AI Provenance Signal score <= 1.0.
- **Broad quality failure** — average score across all dimensions < 2.0.
- **Combined-decline majority** — more than 60% of reviewers individually voted REJECT-or-R&R, but without a majority-REJECT consensus (that would have caught the REJECT branch above).
- **R&R majority** — a clean majority of reviewers individually voted REVISE_AND_RESUBMIT.

### RECOMMEND (accept into the community)
- Average score across all dimensions >= 3.5, AND
- No single dimension below 2.0, AND
- **Domain Fit >= 4.0** — the panel is confident in its competence to evaluate this work end-to-end. Domain Fit in [2.0, 4.0) signals "specialist review needed" or "methodology gap" and routes to REVIEW_FURTHER even when other dimensions clear RECOMMEND.

### REVIEW_FURTHER (panel signals low confidence)
- The default when no other branch fires.
- **Domain Fit between 2.0 and 4.0** — the panel can engage with the work but flags either a methodology gap (DF=2) or specialist-review-needed (DF=3) that the curator should resolve.
- When uncertain, assign REVIEW_FURTHER rather than forcing a binary recommendation.

Note: every recommendation routes to the curator for the final verdict.
REVIEW_FURTHER specifically flags low panel confidence so the curator
weighs the panel's view less heavily; RECOMMEND / REJECT / R&R
recommendations still pass through curator confirmation before any
author email is drafted.

## Novelty Disagreement Flag

If reviewer scores on the **novelty** dimension span 2 or more points (max minus min), this must be explicitly flagged in the combined review output.

Rationale: Large disagreement on novelty is itself a signal. Genuinely original work -- especially work that proposes new frameworks or challenges existing assumptions -- will often be scored high by one reviewer and low by another, because reviewers weight familiarity differently. A novelty disagreement flag is not negative; it indicates the submission requires closer human attention.

Format the flag as: `NOVELTY_DISAGREEMENT: scores ranged [min]-[max] across reviewers (delta = [diff] points). Manual review of novelty assessment recommended.`

## Bias Calibration

The following biases must be actively counteracted during scoring:

- **Do not penalize independent researchers.** Lack of university affiliation, absence of a lab group, or a non-traditional career path are irrelevant to the quality of the work.
- **Do not penalize non-traditional affiliations.** An author affiliated with a small institute, a company, or no institution at all receives the same standard of review as one from a major university.
- **Do not penalize novel frameworks that lack prior literature.** By definition, genuinely new theoretical frameworks will have fewer citations to draw from. Sparse references are expected when the work is proposing something new rather than extending something established. Evaluate the framework on its internal coherence, formal rigor, and explanatory power -- not on the volume of prior art.
- **Do not penalize unconventional structure or presentation** if the content is rigorous. Not all valid research follows the standard IMRaD format.

The explicit purpose of ICSAC is to platform researchers and ideas that traditional gatekeeping systems exclude. Scoring that reproduces those exclusions defeats the institute's reason for existence.

## Reviewer Guidance

When assigning scores, anchor to the scale definitions above, not to a vague sense of quality. A score of 5 means field-advancing -- most competent submissions will score 3 or 4, and that is appropriate. Score inflation (everything gets a 4-5) is as harmful as score deflation (everything gets a 1-2). Calibrate to the definitions.
