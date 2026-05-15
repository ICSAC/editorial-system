# ICSAC Review Rubric: Slop Detection

This document defines red flags for AI-generated, low-effort, or fabricated submissions. Any submission triggering multiple flags below should receive a slop detection score of 1, which results in automatic rejection.

## Red Flags

### Abstract and Framing

- **Generic abstract**: The abstract could describe any paper in the field. It contains no specific claims, no concrete results, and no identifiable contribution. Test: could this abstract be swapped onto a different paper without anyone noticing?
- **Excessive hedging with no concrete claims**: The submission is entirely composed of qualifiers ("may," "could," "potentially," "it is possible that") without ever committing to a specific finding, result, or position.

### Citations and References

- **Fabricated citations**: DOIs that do not resolve, author names that do not appear in any publication database, or journal names that do not exist. Even one fabricated citation is grounds for a slop score of 1.
- **Citation stuffing**: References that are real publications but have no meaningful connection to the submission's content. The reference list exists to create an appearance of scholarship rather than to situate the work.

### Methodology

- **Circular reasoning disguised as methodology**: The submission defines a measure, applies it, and then claims the results validate the measure -- without independent verification or external ground truth.
- **Methodology section that describes no actual method**: The section uses methodological language ("we analyzed," "we computed," "we evaluated") but never specifies what was actually done, on what data, or with what tools.

### Writing Quality

- **Padded word count with no substance**: Paragraphs that restate the same point in different words, filler sentences that add no information, or lengthy introductions that never arrive at a contribution.
- **Perfect grammar but zero domain expertise signals**: The writing is fluent and error-free but contains no specialized terminology, no engagement with known open problems, and no evidence the author has read the literature they cite.

### Structural Signals

- **Uniform section lengths**: Every section is suspiciously similar in length, suggesting template-based generation rather than organic writing driven by content.
- **No engagement with counterarguments or alternative explanations**: The submission presents its claims as though no competing perspectives exist. Genuine researchers in complexity science are aware of debates in their subfield.
- **Figures or tables that do not match the text**: Captions describe results not present in the figure, or figures are generic stock visualizations unrelated to the claimed analysis.

## Reviewer Guidance

No single flag is necessarily disqualifying on its own (except fabricated citations). The slop score reflects the overall pattern. A submission with one minor flag and otherwise solid content should not be penalized heavily. A submission with three or more flags, particularly fabricated citations or a vacuous methodology section, should receive a slop score of 1.

When flagging slop, cite the specific passages or references that triggered the concern. Do not make vague accusations.
