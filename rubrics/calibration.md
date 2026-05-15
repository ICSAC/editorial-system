# ICSAC Review Rubric: Scoring Calibration

This document defines the scoring scale, decision thresholds, and calibration guidance for ICSAC reviews.

## Scoring Scale

Each review dimension (Domain Fit, Methodological Transparency, Internal Consistency, Citation Integrity, Novelty Signal, AI Slop Detection) is scored on a 1-5 scale. Domain Fit has its own dimension-specific rubric in `scope.md` (it scores methodology-bar-and-panel-competence, not topical fit); the rest of this section describes the shared scoring scale used by the other five dimensions.

| Score | Meaning (general dimensions) |
|-------|---------|
| **5** | Exceptional. Field-advancing contribution. Sets a new standard or opens a genuinely new direction. Reserved for work that would be notable at any venue. |
| **4** | Solid and publishable. Minor concerns that do not undermine the core contribution. The work meets professional standards and adds meaningful value to the literature. |
| **3** | Adequate. The work is fundamentally sound but needs revision. Core ideas are viable; execution or presentation has identifiable gaps. |
| **2** | Significant issues. Major revision required. The contribution may be recoverable, but substantial rework is needed in methodology, analysis, or framing. |
| **1** | Fundamentally flawed. The submission has fatal methodological errors or triggers slop detection. (Out-of-scope work — humanities without quantitative method, theology, advocacy — drives Domain Fit to 1 per `scope.md`, not the other dimensions.) |

## Decision Thresholds

### RECOMMEND (publish or publish with minor revision)
- Average score across all dimensions >= 3.5
- No single dimension scored below 2
- **Domain Fit >= 4.0** — the panel is confident in its competence to evaluate this work end-to-end. Domain Fit < 4.0 routes to REVIEW_FURTHER even if other dimensions clear RECOMMEND.

### REJECT
- Any AI Slop Detection score of 1 (automatic rejection, no override)
- Average score across all dimensions below 2.0
- Domain Fit below 2.0 — the work is out of scope (humanities without quantitative method, theology, advocacy, or fails the falsifiability bar per `scope.md`).

### REVIEW_FURTHER (human review required)
- Everything that falls between RECOMMEND and REJECT thresholds.
- **Domain Fit between 2.0 and 4.0** — the panel can engage with the work but flags either a methodology gap (DF=2) or specialist-review-needed (DF=3) that the operator should resolve. The other dimensions still inform the verdict but the Domain Fit signal is load-bearing on its own.
- This is the default for borderline cases. When uncertain, assign REVIEW_FURTHER rather than forcing a binary decision.

## Novelty Disagreement Flag

If the two reviewing models (Claude and Gemini) disagree by 2 or more points on the **novelty** dimension, this must be explicitly flagged in the combined review output.

Rationale: Large disagreement on novelty is itself a signal. Genuinely original work -- especially work that proposes new frameworks or challenges existing assumptions -- will often be scored high by one model and low by another, because the models weight familiarity differently. A novelty disagreement flag is not negative; it indicates the submission requires closer human attention.

Format the flag as: `NOVELTY_DISAGREEMENT: [Model A score] vs [Model B score]. Manual review of novelty assessment recommended.`

## Bias Calibration

The following biases must be actively counteracted during scoring:

- **Do not penalize independent researchers.** Lack of university affiliation, absence of a lab group, or a non-traditional career path are irrelevant to the quality of the work.
- **Do not penalize non-traditional affiliations.** An author affiliated with a small institute, a company, or no institution at all receives the same standard of review as one from a major university.
- **Do not penalize novel frameworks that lack prior literature.** By definition, genuinely new theoretical frameworks will have fewer citations to draw from. Sparse references are expected when the work is proposing something new rather than extending something established. Evaluate the framework on its internal coherence, formal rigor, and explanatory power -- not on the volume of prior art.
- **Do not penalize unconventional structure or presentation** if the content is rigorous. Not all valid research follows the standard IMRaD format.

The explicit purpose of ICSAC is to platform researchers and ideas that traditional gatekeeping systems exclude. Scoring that reproduces those exclusions defeats the institute's reason for existence.

## Reviewer Guidance

When assigning scores, anchor to the scale definitions above, not to a vague sense of quality. A score of 5 means field-advancing -- most competent submissions will score 3 or 4, and that is appropriate. Score inflation (everything gets a 4-5) is as harmful as score deflation (everything gets a 1-2). Calibrate to the definitions.
