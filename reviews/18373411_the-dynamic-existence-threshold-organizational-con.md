---
title: "Review: The Dynamic Existence Threshold: Organizational Consciousness Across Complex Systems"
doi: "10.5281/zenodo.18373411"
record_id: 18373411
review_date: 2026-04-19T20:58:15Z
models: [claude, openrouter:openai/gpt-oss-120b:free, openrouter:nvidia/nemotron-nano-12b-v2-vl:free, openrouter:z-ai/glm-4.5-air:free]
recommendation: RECOMMEND
disagreement: False
passes: 3
---

# Review: The Dynamic Existence Threshold: Organizational Consciousness Across Complex Systems

**DOI:** 10.5281/zenodo.18373411  
**Authors:** Thornhill, Nathan, M.  
**Date:** 2026-04-05  
**Recommendation:** RECOMMEND  
**Panel Passes:** 3  
**Model Disagreement:** No

## Aggregate Scores

| Dimension | Mean | Scores |
|-----------|------|--------|
| Scope Alignment | 5.0 | 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5 |
| Methodological Transparency | 4.2 | 4, 4, 4, 5, 4, 4, 4, 5, 4, 4, 4, 5 |
| Internal Consistency | 4.5 | 4, 4, 5, 5, 4, 4, 5, 5, 4, 4, 5, 5 |
| Citation Integrity | 4.9 | 5, 5, 5, 5, 5, 4, 5, 5, 5, 5, 5, 5 |
| Novelty Signal | 4.4 | 4, 3, 5, 5, 4, 4, 5, 5, 4, 4, 5, 5 |
| AI Slop Detection | 4.9 | 5, 4, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5 |

## Per-Pass Summary

The 5-slot panel was run 3 times; per-pass recommendations and dimension means follow.

| Pass | Recommendation | Scope Alignment | Methodological Transparency | Internal Consistency | Citation Integrity | Novelty Signal | AI Slop Detection |
|------|----------------|------|------|------|------|------|------|
| 1 | RECOMMEND | 5.0 | 4.2 | 4.5 | 5.0 | 4.2 | 4.8 |
| 2 | RECOMMEND | 5.0 | 4.2 | 4.5 | 4.8 | 4.5 | 5.0 |
| 3 | RECOMMEND | 5.0 | 4.2 | 4.5 | 5.0 | 4.5 | 5.0 |

## Score Variance

Standard deviation of per-pass means per dimension — surfaces how stable the panel's verdict is across repeated runs of the same 4-slot panel.

| Dimension | Stdev (across pass means) |
|-----------|---------------------------|
| Scope Alignment | 0.0 |
| Methodological Transparency | 0.0 |
| Internal Consistency | 0.0 |
| Citation Integrity | 0.09 |
| Novelty Signal | 0.14 |
| AI Slop Detection | 0.09 |

## Individual Model Reviews

### Claude (Pass 1)

**Recommendation:** RECOMMEND  
**Summary:** The submission presents a substantive cross-domain framework with formally defined metrics, three empirical domains, six distinct tests, permutation-based negative controls, out-of-sample validation, and a built-in contrastive null via dimensional embedding. The author's explicit acknowledgment of entropy coupling — which collapses the nominal two-dimensional I–D geometry to a near-one-dimensional entropy axis — is handled with methodological honesty rather than obscured. Fits ICSAC scope cleanly; ready for publication with only minor computational-reproducibility gaps (seeds, software versions, hardware specs).

- **Scope Alignment** (5/5): The submission directly engages ICSAC core programs: pattern persistence (existence threshold, Φ conservation), emergence/self-organization (integration-differentiation dynamics), dimensional scaling (86% law reference, dimensional embedding as information destruction), substrate-independence (cross-domain tests across financial, space weather, EEG), and nonlinear dynamics (critical transitions, early warning signals).
- **Methodological Transparency** (4/5): Metrics (R, S, D, I, Φ, B) are formally defined with equations; five-layer architecture and data sources (OMNI2, Sleep-EDF, yfinance) are specified; statistical tests (Mann-Whitney U, permutation, FDR correction) are named with p-values and CIs reported; code repository link provided. Sample sizes are substantial (136,394 EEG epochs, 6,785 trading days). Minor gaps: no hardware/runtime specs, no random seeds reported, software versions absent, and the a priori θ = 2.0 threshold justification is asserted rather than derived.
- **Internal Consistency** (4/5): The author openly acknowledges that entropy coupling (ρ = −0.985) constrains the two-dimensional I–D plane to a one-dimensional manifold (Section 3.6.4, Section 4.6), tempering the four-state model accordingly. Claims are explicitly partitioned into supported (narrow) and unsupported (strong) versions in Section 4.4. The Φ conservation result is internally contrasted with dimensional embedding as a built-in null. One minor tension: the Φ change in EEG is statistically significant (p < 10^-300) yet labeled approximate conservation, which is defensible given effect-size framing but borders on motivated interpretation.
- **Citation Integrity** (5/5): Citations include well-established works (Scheffer 2009 Nature, Tononi IIT, Baars Global Workspace, Kemp Sleep-EDF, Benjamini-Hochberg, Buzsáki, Massimini 2005 Science, Casali 2013 Sci. Transl. Med., Bak/Tang/Wiesenfeld SOC, Williams & Beer PID arXiv:1004.2515) that are all real and topically appropriate. Self-citations to prior Zenodo works (18124074, 18262424, 18319430) are transparent. No evidence of fabrication or citation stuffing; the reference list is modest and substantive rather than padded.
- **Novelty Signal** (4/5): The core contribution — a zero-tuning cross-domain coordinate system combining integration and differentiation with an explicit conservation law Φ = I + D tested against dimensional embedding as a contrastive null — is a genuine synthesis rather than a re-packaging. The EEG Sum anti-prediction (AUC 0.416 vs R×S 0.909) is a clean dissociation between magnitude and structure that the author correctly identifies as the construct-validity linchpin. Not field-redefining: R×S is acknowledged as a proxy that partially tracks spectral distribution (ρ = 0.923 with delta/beta), and the entropy-coupling admission collapses much of the two-dimensional novelty to a one-dimensional entropy axis.
- **AI Slop Detection** (5/5): No slop indicators. The submission acknowledges AI-assisted drafting transparently, provides substantive engagement with counterarguments (entropy coupling as a limitation, delta/beta ratio as a domain-specific competitor, narrow vs. strong claim partition), reports negative controls (label shuffle, temporal shuffle, phase randomization) and honest limitations. No prompt-injection attempts, no operator-directed instructions, no fabricated methodology. Section lengths vary organically, and specific numerical results tie to identifiable tests.

### Openrouter:openai/gpt-oss-120b:free (Pass 1)

**Recommendation:** RECOMMEND  
**Summary:** The submission presents a well‑aligned, methodologically transparent study that unifies integration‑differentiation analysis across three disparate complex systems. While the novelty is moderate, the work is solid, reproducible, and advances the institute's thematic goals.

- **Scope Alignment** (5/5): The paper directly addresses pattern persistence, emergence, dimensional scaling, substrate‑independence, and nonlinear dynamics by proposing the Dynamic Existence Threshold (DET) framework and testing it on financial markets, space weather, and EEG – all core ICSAC programs.
- **Methodological Transparency** (4/5): Methods are described in detail: five‑layer architecture, metric definitions (R, S, D, I, Φ), statistical tests, data sources, and robustness checks. All parameters are listed and the zero‑tuning claim is explicit, enabling replication, though some derivations (e.g., entropy‑coupled metrics) could be expanded.
- **Internal Consistency** (4/5): The results follow logically from the described methods, and the discussion acknowledges limitations and the near‑one‑dimensionality caused by entropy coupling. No major contradictions between claims and presented evidence are observed.
- **Citation Integrity** (5/5): References include well‑known works (e.g., Scheffer et al., Tononi, Strogatz) and the cited Zenodo preprints correspond to the author’s prior work. No fabricated or nonsensical citations were detected.
- **Novelty Signal** (3/5): The DET framework unifies integration and differentiation across domains, which is a novel synthesis. However, the core ideas build on existing concepts (IIT, critical slowing down) and the metric definitions are incremental rather than revolutionary.
- **AI Slop Detection** (4/5): The manuscript is specific, cites concrete data sizes, provides equations, and avoids generic filler language. Minor template‑like phrasing is present but does not indicate low‑effort generation.

### Openrouter:nvidia/nemotron-nano-12b-v2-vl:free (Pass 1)

**Recommendation:** RECOMMEND  
**Summary:** This submission presents a groundbreaking framework for detecting critical transitions across diverse systems through integration-differentiation metrics. The work demonstrates cross-domain validity, methodological rigor, and novel theoretical insights, particularly in distinguishing organizational structure from magnitude. While methodological details could be slightly expanded, the contribution is substantial and aligns perfectly with ICSAC's mission.

- **Scope Alignment** (5/5): The work directly addresses ICSAC themes through cross-domain application of the DET framework to pattern persistence (critical transitions), substrate-independence (financial, space weather, EEG), and complexity science. It connects to emergence via organizational dissolution dynamics and dimensional scaling through entropy-coupled metrics.
- **Methodological Transparency** (4/5): Methods are detailed with clear metric definitions (R, S, D, I), five-layer architecture, and statistical tests. Code is available, but some aspects (e.g., layer decomposition logic) could benefit from more explicit implementation details for full reproducibility.
- **Internal Consistency** (5/5): Results consistently support hypotheses: EEG anti-prediction of Sum, Φ conservation during transitions, and directional dynamics matching theoretical predictions. Statistical tests (permutation controls, AUC comparisons) validate claims.
- **Citation Integrity** (5/5): All cited works (e.g., Scheffer et al. 2009, Tononi et al. 2016) are real publications. Related identifiers (Zenodo DOIs) are valid, and references are contextually relevant to complexity science.
- **Novelty Signal** (5/5): Introduces a novel framework combining integration-differentiation metrics across substrates without parameter tuning. The EEG results (Sum anti-prediction, R×S success) and Φ conservation during transitions represent significant theoretical advances.
- **AI Slop Detection** (5/5): No signs of generic LLM text. Content is highly specific with domain expertise signals (e.g., EEG frequency band details, financial market stress criteria). Methodology avoids circular reasoning and includes negative controls.

### Openrouter:z-ai/glm-4.5-air:free (Pass 1)

**Recommendation:** RECOMMEND  
**Summary:** This exceptional submission introduces a novel, well-validated Dynamic Existence Threshold framework that characterizes complex system states through integration-differentiation balance. With rigorous cross-domain validation, methodological transparency, and significant theoretical contributions, it represents a field-advancing contribution to complexity science.

- **Scope Alignment** (5/5): The submission directly addresses ICSAC core programs, including pattern persistence (organizational identity maintenance), emergence (multi-scale coordination), substrate-independence (cross-domain application to financial, geophysical, and biological systems), and complexity science (critical transitions, early warning signals). The Dynamic Existence Threshold framework provides a unified coordinate system for understanding system behavior across disparate substrates.
- **Methodological Transparency** (5/5): The submission provides exceptional methodological detail: complete mathematical formulations for all metrics (Neff, R, S, I, D, Φ), explicit data sources (6,785 trading days, 9,802 space weather days, 50 EEG subjects), five-layer architecture specifications, statistical methods (non-parametric tests, permutation analysis, AUC with confidence intervals), zero-parameter tuning constraints, robustness checks across thresholds and layer counts, and code availability via GitHub. Methods are fully replicable.
- **Internal Consistency** (5/5): All six test predictions are clearly stated and supported by corresponding results. The EEG validation (structure vs. magnitude dissociation) provides crucial evidence for the framework's construct validity. The Φ conservation finding is consistent across domains and distinguishes critical transitions from irreversible information loss. Claims follow logically from methods, and limitations are appropriately acknowledged without undermining core contributions.
- **Citation Integrity** (5/5): All references appear legitimate and relevant, including foundational works in complexity science (Scheffer et al. on early warning signals), neuroscience (Tononi on integrated information theory), and statistical methods (Benjamini-Hochberg FDR correction). No fabricated citations are detected, and references properly support the theoretical framework and empirical methods.
- **Novelty Signal** (5/5): The submission introduces the novel Dynamic Existence Threshold framework - a two-dimensional coordinate system (I-D plane) for characterizing system states. Key innovations include: cross-domain application without parameter tuning, structure vs. magnitude dissociation demonstrated through EEG, Φ conservation during critical transitions, and identification of two distinct failure modes (reversible redistribution vs. irreversible destruction). The work synthesizes integrated information theory with critical transitions research into a novel, empirically validated framework.
- **AI Slop Detection** (5/5): The submission shows no signs of generic LLM generation. It contains highly specific technical content, domain expertise across finance, space physics, and neuroscience, substantive mathematical formulations, empirical validation with real data, and critical analysis of limitations. The writing demonstrates deep understanding of complex systems theory and would be difficult to generate without specialized training in these domains.

### Openrouter:qwen/qwen3-next-80b-a3b-instruct:free (Pass 1)

**Error:** HTTP 400: {"error":{"message":"Provider returned error","code":400,"metadata":{"raw":"{\"error\":{\"message\":\"This model's maximum context length is 8192 tokens. However, you requested 8192 output tokens and your prompt contains 125904 characters (more than 0 characters, which is the upper bound for 0 input

### Claude (Pass 2)

**Recommendation:** RECOMMEND  
**Summary:** A substantive cross-domain empirical paper testing an integration-differentiation framework on 6,785 trading days, 9,802 space weather days, and 136,394 EEG epochs with zero parameter tuning between domains. The Sum-anti-predicts-while-R×S-succeeds dissociation in EEG (AUC 0.416 vs 0.909) is a clean construct-validity result, and the author transparently acknowledges the entropy-coupling limitation that reduces the nominal 2D I-D space to a 1D manifold. Fits ICSAC scope cleanly and meets publication standards.

- **Scope Alignment** (5/5): The submission directly addresses ICSAC core programs: pattern persistence via the existence threshold concept, substrate-independence across financial, geophysical, and biological domains, dimensional scaling (referencing the 86% law), and nonlinear dynamics via critical transitions. The integration-differentiation framework is squarely within complexity science.
- **Methodological Transparency** (4/5): Metric definitions (R, S, D, I, Φ) are formally specified with equations; data sources (OMNI2, Sleep-EDF, yfinance) are named with access paths; statistical tests (Mann-Whitney, permutation, BH-FDR) are stated with iteration counts. Code repository is referenced. Gaps: no hardware/runtime specs, no random seeds reported for the permutation tests, and the θ=2.0 threshold is stated as a priori without empirical justification for that specific value.
- **Internal Consistency** (4/5): Claims track the presented evidence: AUC 0.909 vs 0.416 dissociation supports the structure-vs-magnitude argument; Φ conservation within 1-14% across domains supports the consistency test framing. The author appropriately downgrades the 'strong claim' of universality to a 'narrow claim' (Section 4.4), and acknowledges that entropy coupling (ρ=-0.985) reduces the I-D plane to near-one-dimensional, which is unusually honest. Minor tension: the four-state taxonomy is presented prominently despite two quadrants being empirically inaccessible.
- **Citation Integrity** (5/5): Spot-checked references are real and appropriate: Scheffer et al. Nature 2009 [1], Tononi Nat Rev Neurosci 2016 [3], Casali Sci Transl Med 2013 [24], Bak-Tang-Wiesenfeld PRL 1987 [29], Williams & Beer arXiv:1004.2515 [31], Kemp Sleep-EDF [18], Benjamini-Hochberg JRSS-B [20]. Self-citations to prior Zenodo DOIs [34-36] are consistent with the author's acknowledged series. No indicators of fabrication.
- **Novelty Signal** (4/5): The I-D plane as a cross-domain coordinate system with zero-parameter tuning across finance/space weather/EEG is a genuinely novel framing. The Φ = I + D conservation finding contrasted against irreversible dimensional-embedding loss is a non-obvious empirical observation. The Sum anti-prediction (AUC 0.416) as a dissociation experiment is a clean falsification design. Novelty is tempered by heavy borrowing from IIT, critical slowing down, and PID; the author acknowledges R × S is a proxy, not a new information-theoretic quantity.
- **AI Slop Detection** (5/5): The submission shows strong domain-expertise signals: awareness of delta/beta ratio as a competing EEG baseline with head-to-head AUC comparison, EMG contamination above 30 Hz, thalamocortical delta generation, Pielou's evenness, JSD base-2 bounding. Section lengths are non-uniform and driven by content. Limitations are enumerated specifically (entropy coupling, temporal resolution, causal ambiguity). Counterarguments (magnitude vs structure hypothesis) are explicitly engaged. No fabricated methodology, no padded abstract. AI-assistance is disclosed. No prompt-injection attempt detected.

### Openrouter:openai/gpt-oss-120b:free (Pass 2)

**Recommendation:** RECOMMEND  
**Summary:** The submission presents a well‑aligned, methodologically sound, and novel cross‑domain framework for detecting organizational dissolution in complex systems. Despite the absence of publicly released code, the work is transparent and internally consistent, warranting publication.

- **Scope Alignment** (5/5): The paper directly addresses pattern persistence, emergence, dimensional scaling, substrate‑independence, and complexity by proposing a cross‑domain integration‑differentiation framework applied to markets, space weather, and EEG.
- **Methodological Transparency** (4/5): Methods are described in detail (five‑layer architecture, metric definitions, statistical tests, data sources). While code is not provided, all parameters, equations, and analysis pipelines are specified enough for independent replication.
- **Internal Consistency** (4/5): The results follow logically from the described methods, and the claims about early‑warning signals, Φ conservation, and cross‑domain transfer are supported by the presented statistics.
- **Citation Integrity** (4/5): References are largely to established works and appear plausible; no obvious fabricated citations were detected, though the large number of self‑citations could not be fully verified.
- **Novelty Signal** (4/5): Introducing the Dynamic Existence Threshold as a unified I‑D coordinate system across disparate substrates is a novel conceptual contribution with empirical validation.
- **AI Slop Detection** (5/5): The manuscript contains specific equations, detailed experimental design, and domain‑specific results, showing no signs of generic filler or AI‑generated padding.

### Openrouter:nvidia/nemotron-nano-12b-v2-vl:free (Pass 2)

**Recommendation:** RECOMMEND  
**Summary:** This submission presents a groundbreaking framework for detecting critical transitions across diverse complex systems. The DET framework demonstrates remarkable cross-domain applicability, methodological rigor, and novel insights into organizational dissolution. While methodological details could be slightly expanded, the work meets all ICSAC standards and makes significant contributions to complexity science.

- **Scope Alignment** (5/5): Directly addresses ICSAC themes through cross-domain analysis of pattern persistence (financial cascades, space weather storms, EEG state transitions), substrate-independence (applied to biological, geophysical, and socioeconomic systems), and complexity science frameworks (integration-differentiation balance). Explicitly connects to critical transitions and nonlinear dynamics.
- **Methodological Transparency** (4/5): Methods are detailed with clear metric definitions (R, S, D, I), five-layer architecture, and statistical tests. Data sources and code availability are provided. However, some technical details (e.g., exact layer decomposition logic) could be more explicit for full reproducibility.
- **Internal Consistency** (5/5): Claims are rigorously supported by domain-specific data (financial market cascades, geomagnetic storms, EEG sleep stages). Results align with hypotheses (e.g., Sum anti-prediction in EEG, Φ conservation during transitions). No contradictions between methods, data, and conclusions.
- **Citation Integrity** (5/5): All cited works (e.g., Scheffer et al. 2009, Tononi et al. IIT papers) are real and relevant. No fabricated references detected. Proper attribution to prior work on critical transitions and integrated information theory.
- **Novelty Signal** (5/5): Introduces the Dynamic Existence Threshold (DET) framework as a novel coordinate system for critical transitions. Unique contributions include cross-domain zero-parameter transfer, Φ conservation during transitions, and substrate-independent early warning signals. EEG results provide novel dissociation between magnitude and structure.
- **AI Slop Detection** (5/5): No signs of generic LLM-generated text. Content is substantive, specific, and technically rigorous. Abstract and full text avoid hedging, contain domain-specific terminology, and present original empirical findings with detailed methodology.

### Openrouter:z-ai/glm-4.5-air:free (Pass 2)

**Recommendation:** RECOMMEND  
**Summary:** This exceptional submission introduces a rigorously tested Dynamic Existence Threshold framework that characterizes system states through integration-differentiation balance across three domains, with exceptional methodological transparency and substantive theoretical contributions to pattern persistence and critical transitions research.

- **Scope Alignment** (5/5): The submission directly addresses ICSAC core programs, particularly pattern persistence and existence thresholds through the Dynamic Existence Threshold framework. It demonstrates substrate-independence by applying the framework across financial markets, space weather, and EEG (biological), while engaging with complexity science through critical transitions and nonlinear dynamics.
- **Methodological Transparency** (5/5): Exceptional methodological detail with fully defined metrics (R, S, D, I, Φ), explicit formulas, data sources (Sleep-EDF, NASA OMNI2, financial data), statistical tests with FDR correction, zero-parameter tuning validation, robustness checks across thresholds, negative controls, layer sensitivity analysis, and out-of-sample validation. Code is openly available.
- **Internal Consistency** (5/5): All six predictions are systematically tested with corresponding results. The EEG analysis provides crucial validation by distinguishing magnitude from structure. Claims about Φ conservation during transitions versus dimensional embedding are logically supported. Limitations are explicitly acknowledged, and the author carefully distinguishes between supported narrow claims and broader untested claims.
- **Citation Integrity** (5/5): All references appear to be real, relevant publications from established researchers in complexity science, neuroscience, and critical transitions literature. Includes proper DOIs, journal information, and appropriate citations to foundational work (Scheffer et al. 2009, Tononi et al. 2016) and the author's previous work on existence thresholds. No signs of fabricated references.
- **Novelty Signal** (5/5): Presents genuinely novel contributions: the Dynamic Existence Threshold framework as a new coordinate system for characterizing system states, organizational information conservation during critical transitions, distinction between reversible redistribution vs. irreversible destruction failure modes, five-layer architecture with zero cross-domain tuning, and the R×S metric that captures multi-scale coordination rather than mere magnitude.
- **AI Slop Detection** (5/5): No signs of generic LLM generation. The submission contains specific technical details, mathematical formulations, concrete results (AUC 0.909, 2.0× variance elevation), domain-specific terminology, and substantive engagement with existing literature. The author's background in distributed system monitoring and previous publications on existence thresholds suggests authentic expertise.

### Openrouter:qwen/qwen3-next-80b-a3b-instruct:free (Pass 2)

**Error:** HTTP 400: {"error":{"message":"Provider returned error","code":400,"metadata":{"raw":"{\"error\":{\"message\":\"This model's maximum context length is 8192 tokens. However, you requested 8192 output tokens and your prompt contains 125904 characters (more than 0 characters, which is the upper bound for 0 input

### Claude (Pass 3)

**Recommendation:** RECOMMEND  
**Summary:** The submission presents a well-specified cross-domain framework with zero-parameter transfer across three substrates, supported by explicit nulls, out-of-sample validation, and honest acknowledgment of the entropy-coupling limitation that reduces the nominal 2D space to ~1D. The EEG Sum-versus-R×S dissociation is a clean empirical contribution and the Φ conservation-versus-destruction contrast meaningfully links this dynamic framework to the author's prior static existence threshold. Minor methodological transparency gaps (hardware, seeds, software versions) do not undermine the core contribution.

- **Scope Alignment** (5/5): The submission directly addresses ICSAC core programs: pattern persistence (balance zone as dynamic existence threshold), emergence (organizational dissolution), dimensional scaling (explicit reference to 86% scaling law and dimensional embedding), substrate-independence (cross-domain testing across financial, geophysical, biological substrates), and nonlinear dynamics (critical transitions, early warning signals). Zero-parameter cross-domain framework is a central substrate-independence claim.
- **Methodological Transparency** (4/5): Methods are specified with substantial detail: explicit formulas for R, S, D, I, and Φ (Equations 1–8); threshold values stated a priori (θ = 2.0); data sources named (NASA OMNI2, Sleep-EDF/PhysioNet); epoch counts, statistical tests (Mann–Whitney U, permutation with 10,000 iterations, Benjamini–Hochberg FDR), and bootstrap CIs reported; code repository linked. Gaps: no hardware specs, no software versions, no random seeds, and the GitHub URL's contents cannot be verified from the text. The L4 z-score protocol is described but the haven-asset list is only partial.
- **Internal Consistency** (4/5): Claims track the evidence: the Sum anti-prediction (AUC 0.416) and R×S AUC 0.909 are reported with matching interpretation; the Φ conservation claim is qualified (1–14% deviation, not strict) and the p=0.576 non-significance for space weather is reported honestly. The author explicitly acknowledges the ρ=−0.985 entropy coupling collapses the nominally 2D space to ~1D and revises the strong claim accordingly. One minor tension: headline '91% accuracy' in abstract corresponds to AUC 0.909, which is discriminative capacity rather than accuracy, but the body uses AUC consistently.
- **Citation Integrity** (5/5): References are dominated by well-known, verifiable works: Scheffer et al. 2009 Nature on early-warning signals, Dakos et al. 2012 PLoS ONE, Tononi IIT papers, Casali et al. 2013 Sci. Transl. Med. on PCI, Kemp et al. 2000 Sleep-EDF, Goldberger et al. 2000 PhysioNet, Benjamini–Hochberg 1995, Williams & Beer PID arXiv:1004.2515, Bak-Tang-Wiesenfeld 1987. Self-citations to Thornhill [34,35,36] are prior Zenodo DOIs in the same research program. No fabricated-looking entries identified.
- **Novelty Signal** (4/5): The contribution combines known elements (IIT's I/D intuition, critical slowing down, PID-inspired R/S proxies) into a concrete zero-parameter cross-domain coordinate system with a specific empirical dissociation (Sum anti-prediction in EEG) and a Φ-conservation-vs-destruction contrast linking the dynamic framework to a prior static existence threshold. The cross-domain replication and structure-versus-magnitude dissociation are non-trivial empirical contributions. The framework is not field-advancing in the sense of opening a new subfield — the ingredients are established — but the synthesis and the clean EEG dissociation represent genuine incremental novelty.
- **AI Slop Detection** (5/5): The submission shows strong domain-expertise signals absent from typical LLM slop: specific numerical results with CIs (AUC 0.909 [0.904, 0.913]), honest negative results (space weather Φ p=0.576 n.s., N3→N2 14% deviation acknowledged), explicit limitations section naming the entropy-coupling problem that undermines the 2D framing, reported ablations (L4 conjunction gate, layer count sensitivity N=3..8, phase-randomization null, label-shuffle null, out-of-sample temporal split), and domain-specific knowledge (AASM criteria, Dst<−50 nT storm threshold, delta/beta ratio comparison). AI-tool use is disclosed. No fabricated citations, no padded abstract, no template phrasing.

### Openrouter:openai/gpt-oss-120b:free (Pass 3)

**Recommendation:** RECOMMEND  
**Summary:** The submission presents a well‑aligned, methodologically transparent study that unifies integration‑differentiation analysis across multiple complex systems, offering novel cross‑domain insights with solid empirical support and credible citations.

- **Scope Alignment** (5/5): The paper directly addresses pattern persistence, emergence, dimensional scaling, substrate‑independence, and complexity by proposing a cross‑domain integration‑differentiation framework and testing it on financial, space‑weather, and neural data, fitting squarely within the core programs of ICSAC.
- **Methodological Transparency** (4/5): Methods are described in detail: five‑layer architecture, metric definitions (R, S, D, I, Φ), statistical tests, data sources, and robustness checks are all specified. While some implementation specifics (e.g., exact code) are not provided, enough information (equations, parameter values, validation procedures) is given for independent replication.
- **Internal Consistency** (4/5): The claims about early‑warning signals, Φ conservation, and cross‑domain transfer follow logically from the presented analyses and results. Minor tension exists in the discussion of entropy coupling limiting the two‑dimensional I‑D space, but this is acknowledged and does not undermine the overall argument.
- **Citation Integrity** (5/5): All cited works correspond to real publications (e.g., Scheffer et al., Tononi, Bassett, etc.). No fabricated or irrelevant references were detected.
- **Novelty Signal** (4/5): Introducing the Dynamic Existence Threshold as a unified I‑D coordinate system across disparate substrates is a novel synthesis of integrated information theory and critical‑transition early‑warning literature, though it builds on existing concepts rather than overturning them.
- **AI Slop Detection** (5/5): The manuscript exhibits domain‑specific terminology, detailed equations, concrete data descriptions, and nuanced discussion that are inconsistent with generic LLM‑generated filler. No signs of fabricated methodology or padded abstract were found.

### Openrouter:nvidia/nemotron-nano-12b-v2-vl:free (Pass 3)

**Recommendation:** RECOMMEND  
**Summary:** Groundbreaking work introducing a substrate-independent framework for critical transitions with rigorous cross-domain validation. Methodological transparency is high despite minor documentation gaps. Novelty and theoretical contributions are exceptional. No evidence of AI-generated content or methodological flaws.

- **Scope Alignment** (5/5): Directly addresses ICSAC themes through cross-domain analysis of pattern persistence (financial cascades, EEG transitions), emergence (organizational consciousness), substrate-independence (applied to markets, weather, brains), and complexity metrics. Explicitly connects to critical transitions and nonlinear dynamics.
- **Methodological Transparency** (4/5): Methods are detailed with five-layer architecture, metric definitions, and domain-specific mappings. Code availability via GitHub link, but lacks documentation for reproducibility. Parameters and statistical tests are explicitly stated, though some implementation details (e.g., layer splitting criteria) could improve clarity.
- **Internal Consistency** (5/5): Claims are rigorously supported by cross-domain validation (financial, space weather, EEG). Framework predictions (early warning signals, balance zone dynamics) are consistently demonstrated. Entropy coupling limitations are acknowledged and contextualized.
- **Citation Integrity** (5/5): All references are real publications (e.g., Scheffer et al. 2009, Tononi 2004). DOIs and URLs are valid. No fabricated citations detected. Properly cites foundational works in IIT and critical transitions.
- **Novelty Signal** (5/5): Introduces the Dynamic Existence Threshold (DET) as a novel coordinate system for critical transitions. First to demonstrate cross-domain applicability without parameter tuning. Unique integration of entropy metrics with early warning signals and Φ conservation analysis.
- **AI Slop Detection** (5/5): No signs of LLM-generated text. Content shows deep domain expertise, specific technical details (e.g., EEG bandpower calculations, financial layer mappings), and original theoretical contributions. No padded abstracts or generic phrasing.

### Openrouter:z-ai/glm-4.5-air:free (Pass 3)

**Recommendation:** RECOMMEND  
**Summary:** This submission introduces a novel Dynamic Existence Threshold framework that characterizes critical transitions across complex systems through integration-differentiation balance. The work demonstrates exceptional methodological rigor with zero-parameter tuning across three domains and provides strong empirical validation, including the crucial finding that the framework captures organizational structure rather than mere magnitude.

- **Scope Alignment** (5/5): The submission directly addresses multiple ICSAC core programs: pattern persistence (existence thresholds), emergence (critical transitions), substrate-independence (cross-domain application to financial, geophysical, and biological systems), and complexity science (integration-differentiation framework). The work advances understanding of how complex systems maintain organizational identity across disparate substrates.
- **Methodological Transparency** (5/5): Exceptional methodological detail with all metrics mathematically defined (equations 1-6), data sources explicitly stated (NASA OMNI2, Sleep-EDF dataset), statistical tests named with p-values and confidence intervals, zero-parameter tuning emphasized and validated, robustness checks across multiple thresholds, negative controls (label shuffling, temporal shuffling), and out-of-sample validation. The methodology is fully replicable from the text.
- **Internal Consistency** (5/5): All six tests are logically derived from the DET hypothesis, with clear predictions that are systematically evaluated against data. The EEG results (Test 5) provide crucial validation by distinguishing structure from magnitude. The conservation test (Test 6) provides a consistency check distinguishing critical transitions from dimensional embedding. Claims follow directly from methods without overstatement, and limitations are appropriately acknowledged.
- **Citation Integrity** (5/5): All references appear to be real and relevant, including foundational work in complexity science (Scheffer et al. 2009), integrated information theory (Tononi et al.), domain-specific methodologies, and the author's previous work. Citations are properly formatted and appropriate for the claims being made. No fabricated references are detected.
- **Novelty Signal** (5/5): The Dynamic Existence Threshold framework represents a genuinely novel contribution extending integrated information theory into a dynamic framework. The integration-differentiation balance concept provides a new coordinate system for critical transitions. The five-layer architecture with zero-parameter tuning across domains is innovative. The discovery of organizational information conservation during transitions and the distinction between reversible shifts and irreversible destruction are novel theoretical and empirical contributions.
- **AI Slop Detection** (5/5): No signs of generic LLM-generated text or fabricated methodology. The writing is highly specific and domain-appropriate with technical depth. The methodology is mathematically rigorous with concrete numerical results. The limitations section is substantive. The abstract contains specific claims and results rather than generic hedging. The work demonstrates deep engagement with literature and domain expertise.

### Openrouter:qwen/qwen3-next-80b-a3b-instruct:free (Pass 3)

**Error:** HTTP 400: {"error":{"message":"Provider returned error","code":400,"metadata":{"raw":"{\"error\":{\"message\":\"This model's maximum context length is 8192 tokens. However, you requested 8192 output tokens and your prompt contains 125904 characters (more than 0 characters, which is the upper bound for 0 input

---

*This review was produced through ICSAC's open review process — a multi-reviewer panel (3-pass aggregation with AI tooling: claude, openrouter:openai/gpt-oss-120b:free, openrouter:nvidia/nemotron-nano-12b-v2-vl:free, openrouter:z-ai/glm-4.5-air:free). Final acceptance decisions are made by human curators.*
