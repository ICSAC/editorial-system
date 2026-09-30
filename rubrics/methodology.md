# ICSAC Review Rubric: Methodology

This document defines what ICSAC considers transparent, rigorous methodology. Score methodology based on how well the submission meets the applicable standards below.

## Reproducibility

- **Data availability**: The submission must state where data can be obtained. Proprietary data is acceptable only if the methodology can be independently verified on comparable datasets.
- **Code availability**: Computational work must provide source code, pseudocode, or sufficient algorithmic detail for independent reimplementation. A GitHub link with no documentation does not satisfy this requirement.
- **Explicit parameters**: All model parameters, hyperparameters, thresholds, and configuration values must be stated. If parameters were tuned, the tuning procedure must be described.

## Mathematical Rigor

- Proofs must be verifiable step-by-step. Hand-waving ("it can be shown that...") without supporting derivation is a methodological deficiency.
- Assumptions must be stated explicitly. Hidden assumptions in proofs or derivations should be flagged.
- **Assumptions must hold where the paper applies the result.** A theorem whose assumptions the paper's own examples violate (a continuity or density assumption applied where it cannot hold; an independence assumption applied to coupled data) does not support those examples. Name the assumption and the example.
- Novel notation must be defined at first use.
- Proof checking is a curator step: the reviewer panel judges whether a proof is presented verifiably and whether its assumptions are stated and met; the curation team checks the proof itself before a decision.

## Claims About Code

- A claim about what the submission's code does ("the script computes X", "the analysis operationalizes the measure defined in §N", "reproducible", "holds by construction") is a claim about the code, and must be checked against the code package digest and cited to a file and line. A paper whose shipped code computes something other than the construct it names has not operationalized that construct.
- Where no code package could be read, a claim about the code is unverified, not verified.

## Empirical Work

- **Sample sizes** must be reported and justified relative to the claims being made.
- **Statistical tests** must be named, with test statistics and p-values reported. Non-parametric alternatives should be used or justified when distributional assumptions are questionable.
- **Confidence intervals** or equivalent uncertainty quantification must accompany point estimates.
- **Effect sizes** should be reported alongside significance tests.

## Computational Work

- **Hardware specifications**: processor, memory, GPU model if applicable.
- **Runtime**: wall-clock time for key experiments, or at minimum order-of-magnitude estimates.
- **Seed values**: random seeds must be reported or the submission must demonstrate robustness across multiple seeds.
- **Software versions**: language version, key library versions, operating system.

## Honesty and Limitations

- **Negative results** must be reported, not omitted. A submission that presents only favorable outcomes without acknowledging failures or boundary conditions is methodologically incomplete.
- **Limitations** must be stated explicitly in a dedicated section or clearly within the discussion. Overstating conclusions relative to the evidence is a scoring penalty.

## Novel Metrics and Measures

- Any new metric, measure, or quantity introduced by the submission must be **formally defined** with mathematical precision.
- The measure's properties (boundedness, monotonicity, sensitivity, edge cases) should be characterized.
- Comparison to existing measures, where applicable, strengthens the contribution.

## Reviewer Guidance

Score methodology based on the standards applicable to the submission type. A purely theoretical paper is not penalized for lacking hardware specs. A purely empirical paper is not penalized for lacking formal proofs. Apply the relevant criteria and assess whether the work, within its own domain, meets the transparency and rigor standards described above.
