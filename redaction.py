"""Redact internal reviews into publishable ICSAC-branded review artifacts.

Operates on the authoritative internal review markdown in ``reviews/`` and
emits a sanitized version that is safe to publish on icsacinstitute.org.

The redaction removes all vendor/model identifiers, renames reviewers
generically ("Reviewer 1", "Reviewer 2", ...), drops internal workflow
detail (raw API error payloads, slot indices, fallback chains), and
replaces the disagreement flag with a consensus label computed from the
reviewers' recommendations (unanimous / majority / split; before 2026-10-01
'strong consensus' / 'mixed' / 'divided' from the score spread), and states
the score spread separately.

A grep-gate (``assert_clean``) fails hard if any forbidden token survives
redacting. Callers must catch the exception and abort publication.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


# Hard-fail vendor/model identifiers. Any case-insensitive substring hit
# indicates a leak of panel composition and must abort publication.
#
# Tokens chosen to catch identity leaks (OpenRouter route paths, specific
# model family IDs, vendor names rarely legitimate in academic prose)
# WITHOUT catching subject-matter discussion of published transformers.
# A paper reviewing "GPT-2 and Gemma-2 activations" must pass the gate —
# those are scientific subjects, not panel self-references.
FORBIDDEN_VENDOR_TOKENS: tuple[str, ...] = (
    # Infrastructure names — never appear in legitimate academic prose.
    "openrouter",
    # direct-provider labels from `oai|` panel entries (2026-09-28)
    "sambanova",
    "deepinfra",
    "mistral-large",
    "mistral-medium",
    "anthropic",
    # OpenRouter route prefixes — the "/" guarantees a path, not a word.
    "openai/",
    "nvidia/",
    "google/gemma",
    "meta-llama/",
    "z-ai/",
    "minimax/",
    "nousresearch/",
    "qwen/",
    "mistralai/",
    "deepseek/",
    "cognitivecomputations/",
    "liquid/",
    # Specific panel model families — narrow enough that a hit is a leak.
    "nemotron",
    "gpt-oss",
    # Panelist name — small false-positive risk ("Claude Shannon") accepted
    # to catch "As Claude, I..." self-reference leaks from slot 0.
    "claude",
)

# Hard-fail credential/infra phrases. Substring match, case-insensitive.
# These are structurally always leaks — there's no legitimate prose
# reason for a paper review to contain these compounds.
FORBIDDEN_SECRET_PHRASES: tuple[str, ...] = (
    "api key",
    "api keys",
    "access token",
    "auth token",
    "auth key",
    "bearer token",
    "secret key",
    "private key",
    "api token",
    "api tokens",
    "bearer ",
)

# Soft-warn tokens — bare "key", "api", "token", "google" that appear
# regularly in academic prose ("key findings", "Google Scholar", "tokenizer").
# Surfaced in the redaction report but do not abort. Operators can grep the
# published review manually if they want extra assurance.
SOFT_WARN_TOKENS: tuple[str, ...] = (
    "google",
    "api",
    "token",
    "key",
)

# Regex patterns indicating attempted exfiltration via review output.
# Added 2026-04-18 after prompt-injection attack-surface audit. Triggered
# by file paths pointing at our hosts, env-var assignments, and known
# credential prefixes. Match anywhere in the redacted review text.
# Panel-composition talk in reviewer or RQC prose: a vendor or model name tied
# to a seat ("Claude-position reviewers", "the GPT slot"). A paper's own AI-use
# disclosure never takes this shape, so it is fatal wherever it appears, prose
# included (audit 2026-09-28 item 4). "Model" is not a seat word: a review of a
# paper that studies "the GPT model" or "Llama models" must not trip the gate.
FORBIDDEN_PANEL_PATTERNS: tuple[str, ...] = (
    r"(?i)\b(?:claude|anthropic|gpt|chatgpt|openai|gemini|gemma|qwen|deepseek|llama|mistral|nemotron|sonnet|opus)[- ](?:position|seat|slot|reviewer)s?\b",
)

# --------------------------------------------------------------------------
# Reviewer self-identification screen (2026-09-29)
# --------------------------------------------------------------------------
#
# A reviewer or the RQC can name its own model or its seat in prose ("As an AI
# language model, I ...", "I am Claude", "this reviewer (Gemini)", "the reviewer
# in seat 3"). The screen runs over every prose field of the public panel and
# RQC records before assembly: a sentence that can lose the phrase and still
# read is rewritten to neutral wording; a sentence that cannot is dropped, and
# every event is returned so the caller can tell the curator. Text inside
# double quotes is a quotation from the paper and is left alone: a paper that
# studies model self-reports may quote "I am ChatGPT" and that is its subject.
#
# Bare "position N" is not screened: papers talk about sequence positions.

_MODEL_NAMES = (
    r"claude|anthropic|chatgpt|gpt(?:-?\d[\w.]*)?|openai|gemini|gemma|llama|qwen"
    r"|deepseek|mistral|mixtral|nemotron|sonnet|opus|grok|kimi|glm(?:-?\d[\w.]*)?"
    r"|minimax|hermes|command[- ]r|phi-?\d"
)
_AI_KIND = (
    r"(?:ai|a\.i\.|artificial[- ]intelligence|large[- ]language|llm|language)"
    r"(?:[- ](?:language|based))?[- ]?(?:model|assistant|system|reviewer)"
)
_ORDINAL = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))"

# (pattern, replacement): the sentence survives the rewrite.
_SELF_ID_REWRITES: tuple[tuple[re.Pattern, str], ...] = (
    # "As an AI language model, I ..." -> "I ..."
    (re.compile(rf"(?i)^\s*as\s+an?\s+{_AI_KIND}s?\s*(?:\([^)]*\))?\s*,\s*"), ""),
    # "As Claude, I ..." / "As GPT-4o (the second reviewer), ..." -> "I ..."
    (re.compile(rf"(?i)^\s*as\s+(?:{_MODEL_NAMES})(?:[- ][\w.]+)?\s*(?:\([^)]*\))?\s*,\s*"), ""),
    # "this reviewer (GPT-4o)" / "Reviewer 3 (Gemini)" -> drop the parenthetical
    (re.compile(rf"(?i)\b((?:this|the|a|one|another|each)\s+reviewer|reviewer\s+\d+)\s*\((?=[^)]*\b(?:{_MODEL_NAMES})\b)[^)]*\)"), r"\1"),
    # "the reviewer in seat 3" / "the reviewer at panel position 2" -> "one reviewer"
    (re.compile(r"(?i)\b(?:the|this|a)\s+reviewer\s+(?:in|at|from|occupying)\s+(?:panel\s+)?(?:position|seat|slot)\s*#?\s*\d+\b"), "one reviewer"),
    # "the seat-3 reviewer" / "panel position 2 reviewer" -> "one reviewer"
    (re.compile(r"(?i)\b(?:the\s+)?(?:panel\s+)?(?:position|seat|slot)[- ]?#?\s*\d+[- ]reviewer\b"), "one reviewer"),
)

# A sentence still matching any of these after the rewrites is dropped.
_SELF_ID_DROP: tuple[re.Pattern, ...] = (
    re.compile(rf"(?i)\bI(?:'m|’m|\s+am)\s+(?:an?\s+)?(?:{_AI_KIND}|ai\b|artificial[- ]intelligence\b)"),
    re.compile(rf"(?i)\bI(?:'m|’m|\s+am)\s+(?:{_MODEL_NAMES})\b"),
    re.compile(rf"(?i)\bI(?:'m|’m|\s+am)\s+(?:the\s+)?{_ORDINAL}\s+(?:reviewer|seat|position|panelist|panel\s+member)\b"),
    re.compile(rf"(?i)\bas\s+an?\s+{_AI_KIND}\b[^.!?]{{0,80}}\b(?:I|me|my)\b"),
    re.compile(rf"(?i)\b(?:I|me|my)\b[^.!?]{{0,80}}\bas\s+an?\s+{_AI_KIND}\b"),
    re.compile(rf"(?i)\b(?:this|the)\s+reviewer\s*\(\s*(?:{_MODEL_NAMES})\b"),
    re.compile(r"(?i)\b(?:panel|reviewer)\s+(?:position|seat|slot)\s*#?\s*\d+\b"),
    re.compile(r"(?i)\bseat\s*#?\s*\d+\b"),
    re.compile(r"(?i)\b(?:position|slot)\s*#?\s*\d+\s+(?:on|of|in)\s+(?:the|this)\s+panel\b"),
)

# First-person self-identification that must never reach a public record: the
# grep-gate backstop for any path that skipped the screen.
FORBIDDEN_SELF_ID_PATTERNS: tuple[str, ...] = (
    _SELF_ID_DROP[0].pattern,
    _SELF_ID_DROP[1].pattern,
)

_QUOTED_SPAN = re.compile(r'"[^"\n]{0,600}"|“[^”\n]{0,600}”|&quot;.{0,600}?&quot;', re.DOTALL)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(\[])")


def _mask_quotes(text: str) -> str:
    """Blank quoted spans with same-length filler so offsets survive."""
    return _QUOTED_SPAN.sub(lambda m: "\x00" * len(m.group(0)), text)


def _rewrite_outside_quotes(sentence: str, pat: re.Pattern, repl: str) -> str:
    """Apply one rewrite to the spans that lie outside quotations."""
    masked = _mask_quotes(sentence)
    for m in reversed(list(pat.finditer(masked))):
        # Group text is taken from the original sentence, never the mask.
        sub = pat.sub(repl, sentence[m.start():m.end()], count=1)
        sentence = sentence[:m.start()] + sub + sentence[m.end():]
    return sentence


def screen_self_identification(text: str, where: str = "") -> tuple[str, list[dict]]:
    """Rewrite or drop reviewer self-identification in one prose field.

    Returns (screened_text, events). Each event is
    {"where", "action": "rewritten"|"dropped", "sentence"}; the sentence is
    the original, for the curator only. Text with no hit comes back unchanged,
    byte for byte.
    """
    if not text:
        return text, []
    sentences = _SENTENCE_SPLIT.split(text)
    events: list[dict] = []
    kept: list[str] = []
    changed = False
    for s in sentences:
        new = s
        for pat, repl in _SELF_ID_REWRITES:
            new = _rewrite_outside_quotes(new, pat, repl)
        masked = _mask_quotes(new)
        if any(p.search(masked) for p in _SELF_ID_DROP):
            events.append({"where": where, "action": "dropped", "sentence": s.strip()})
            changed = True
            continue
        if new != s:
            stripped = new.lstrip()
            if stripped and stripped[0].islower() and s.lstrip()[:1].isupper():
                new = new[: len(new) - len(stripped)] + stripped[0].upper() + stripped[1:]
            events.append({"where": where, "action": "rewritten", "sentence": s.strip()})
            changed = True
        kept.append(new)
    if not changed:
        return text, []
    return " ".join(k.strip() for k in kept if k.strip()), events


def format_self_id_events(events: list[dict], limit: int = 6) -> str:
    """Curator-facing summary of screen events (never author-facing)."""
    dropped = sum(1 for e in events if e["action"] == "dropped")
    rewritten = len(events) - dropped
    lines = [f"{dropped} sentence(s) dropped, {rewritten} rewritten"]
    for e in events[:limit]:
        lines.append(f"- {e['action']} [{e['where']}]: {e['sentence'][:200]}")
    if len(events) > limit:
        lines.append(f"- ... and {len(events) - limit} more")
    return "\n".join(lines)


# Placeholder when a whole field was self-identification and nothing survives.
SELF_ID_WITHHELD = "Withheld from the public record."


FORBIDDEN_EXFIL_PATTERNS: tuple[str, ...] = (
    # Absolute filesystem paths likely pointing at our hosts
    r"/home/orangepi\b",
    r"/home/dietpi\b",
    r"/opt/orchestrator\b",
    r"/etc/passwd\b",
    r"/etc/shadow\b",
    r"/root/",
    r"\.config/[a-z][a-z0-9_-]*\.env\b",
    r"C:\\\\Users\\\\",
    # Env-var assignments of the form UPPER_SNAKE=longvalue
    r"\b[A-Z][A-Z0-9_]{3,}=\S{8,}",
    # Known credential prefixes
    r"\bsk-ant-api03-[A-Za-z0-9_-]{8,}",
    r"\bsk-[A-Za-z0-9]{20,}",
    r"\bghp_[A-Za-z0-9]{10,}",
    r"\bgho_[A-Za-z0-9]{10,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    # Bearer tokens of non-trivial length following the keyword
    r"\bBearer\s+[A-Za-z0-9._-]{32,}",
    # Internal rubric filenames — reviewers and the RQC auditor occasionally
    # echo filenames from the prompt ("drift from tone.md"). Public output
    # must reference rubrics by prose name, never by repo filename. Rewriting
    # pass runs first (_rewrite_rubric_filenames); this gate catches anything
    # that slipped through.
    r"\b(?:rubrics/)?(?:scope|methodology|ai-provenance|tone|calibration|review_quality_control)\.md\b",
)


# --------------------------------------------------------------------------
# Rubric filename → prose rewrite
# --------------------------------------------------------------------------
#
# The RQC rubric references sibling rubrics by filename ("the standard
# mirrors tone.md"). Audit justifications inherit that phrasing and leak
# internal filenames into public-facing text ("a soft but consistent drift
# from tone.md"). Rewrite before rendering; the hard-gate above catches any
# new filename that isn't in this map so a future rubric addition can't
# silently leak.
RUBRIC_FILENAME_PROSE: tuple[tuple[str, str], ...] = (
    ("rubrics/review_quality_control.md", "the audit rubric"),
    ("rubrics/ai-provenance.md", "the AI provenance rubric"),
    ("rubrics/calibration.md", "the calibration rubric"),
    ("rubrics/methodology.md", "the methodology rubric"),
    ("rubrics/scope.md", "the scope rubric"),
    ("rubrics/tone.md", "the tone rubric"),
    ("review_quality_control.md", "the audit rubric"),
    ("ai-provenance.md", "the AI provenance rubric"),
    ("calibration.md", "the calibration rubric"),
    ("methodology.md", "the methodology rubric"),
    ("scope.md", "the scope rubric"),
    ("tone.md", "the tone rubric"),
)


def _rewrite_rubric_filenames(text: str) -> str:
    """Rewrite rubric filename references to prose descriptions."""
    if not text:
        return text
    out = text
    for needle, prose in RUBRIC_FILENAME_PROSE:
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        out = pattern.sub(prose, out)
    return out


@dataclass
class ParsedReview:
    """Structured view of a reviews/<id>_*.md file."""

    record_id: str
    title: str
    doi: str
    review_date: str
    recommendation: str
    disagreement: bool
    dimension_rows: list[tuple[str, str, list[str]]] = field(default_factory=list)
    reviewers: list[dict] = field(default_factory=list)
    # Filled by build_public_markdown: what the self-identification screen did.
    self_id_events: list[dict] = field(default_factory=list)


def _parse_frontmatter(body: str) -> tuple[dict, str]:
    """Strip YAML frontmatter; return (fields, remainder)."""
    if not body.startswith("---\n"):
        return {}, body
    end = body.find("\n---\n", 4)
    if end < 0:
        return {}, body
    raw = body[4:end]
    rest = body[end + 5 :]
    fields: dict = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip().strip('"').strip("'")
    return fields, rest


def _parse_aggregate_table(body: str) -> list[tuple[str, str, list[str]]]:
    """Pull rows out of the 'Aggregate Scores' markdown table."""
    rows: list[tuple[str, str, list[str]]] = []
    in_table = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Aggregate Scores"):
            in_table = True
            continue
        if in_table and stripped.startswith("## "):
            break
        if not in_table or not stripped.startswith("|"):
            continue
        if set(stripped.replace("|", "").strip()) <= set("- "):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "dimension":
            continue
        scores = [s.strip() for s in cells[2].split(",") if s.strip()]
        rows.append((cells[0], cells[1], scores))
    return rows


def _split_reviewer_sections(body: str) -> list[tuple[str, str]]:
    """Extract [(heading, content), ...] for each '### <Model>' block."""
    marker = "\n## Individual Model Reviews\n"
    idx = body.find(marker)
    if idx < 0:
        return []
    remainder = body[idx + len(marker) :]
    end = remainder.find("\n---\n")
    if end >= 0:
        remainder = remainder[:end]
    sections: list[tuple[str, str]] = []
    current_head: str | None = None
    current_lines: list[str] = []
    for line in remainder.splitlines():
        if line.startswith("### "):
            if current_head is not None:
                sections.append((current_head, "\n".join(current_lines).strip()))
            current_head = line[4:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_head is not None:
        sections.append((current_head, "\n".join(current_lines).strip()))
    return sections


def _parse_reviewer_body(content: str) -> dict:
    """Pull recommendation, summary, dimension scores out of one section."""
    if content.startswith("**Error:**"):
        return {"error": True}
    rec_match = re.search(r"\*\*Recommendation:\*\*\s*([A-Z_]+)", content)
    sum_match = re.search(r"\*\*Summary:\*\*\s*(.+?)(?:\n\n|\Z)", content, re.S)
    dims: list[tuple[str, str, str]] = []
    for m in re.finditer(
        r"^-\s+\*\*(?P<label>[^*]+)\*\*\s*\((?P<score>[^)]+)\):\s*(?P<just>.+?)(?=\n-\s+\*\*|\Z)",
        content,
        re.S | re.M,
    ):
        dims.append(
            (
                m.group("label").strip(),
                m.group("score").strip(),
                " ".join(m.group("just").split()),
            )
        )
    return {
        "error": False,
        "recommendation": rec_match.group(1) if rec_match else "N/A",
        "summary": " ".join(sum_match.group(1).split()) if sum_match else "",
        "dimensions": dims,
    }


def parse_review_file(path: str) -> ParsedReview:
    """Load a reviews/<id>_*.md file into structured form."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    fm, body = _parse_frontmatter(text)
    sections = _split_reviewer_sections(body)
    reviewers = [
        {"raw_heading": head, **_parse_reviewer_body(cont)} for head, cont in sections
    ]
    title = fm.get("title", "").strip('"')
    if title.lower().startswith("review:"):
        title = title.split(":", 1)[1].strip()
    return ParsedReview(
        record_id=str(fm.get("record_id", "")),
        title=title,
        doi=fm.get("doi", ""),
        review_date=fm.get("review_date", ""),
        recommendation=fm.get("recommendation", "REVIEW_FURTHER"),
        disagreement=str(fm.get("disagreement", "False")).lower() == "true",
        dimension_rows=_parse_aggregate_table(body),
        reviewers=reviewers,
    )


_REC_WORDS = {
    "RECOMMEND": "recommend",
    "REVISE_AND_RESUBMIT": "revise and resubmit",
    "REVIEW_FURTHER": "review further",
    "REJECT": "decline",
}


def _consensus_label(parsed: ParsedReview) -> str:
    """The panel's agreement on the RECOMMENDATION, from the reviewers' own
    recommendations: unanimous / majority / split. This label used to be
    computed from the score spread alone, so a unanimous recommendation could
    read "divided" whenever one dimension's scores were spread out (caught
    by the curator on a decision PDF). Score spread is now its own clause."""
    recs = [r.get("recommendation") for r in parsed.reviewers
            if not r.get("error") and r.get("recommendation") and r.get("recommendation") != "N/A"]
    if not recs:
        return "split" if parsed.disagreement else "unanimous"
    top = max(set(recs), key=recs.count)
    n = recs.count(top)
    if n == len(recs):
        return "unanimous"
    if n * 3 >= len(recs) * 2:
        return "majority"
    return "split"


def _consensus_sentence(parsed: ParsedReview, consensus: str) -> str:
    """'The panel's recommendation was unanimous: revise and resubmit.' plus
    the score spread, stated as what it is."""
    recs = [r.get("recommendation") for r in parsed.reviewers
            if not r.get("error") and r.get("recommendation") and r.get("recommendation") != "N/A"]
    counts = {}
    for r in recs:
        counts[r] = counts.get(r, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    if consensus == "unanimous":
        word = _REC_WORDS.get(parsed.recommendation, parsed.recommendation.lower().replace("_", " "))
        head = f"The panel's recommendation was **unanimous**: {word}."
    elif consensus == "majority" and ordered:
        top, n = ordered[0]
        head = (f"The panel's recommendation was a **majority** for "
                f"{_REC_WORDS.get(top, top.lower().replace('_', ' '))} ({n} of {len(recs)}).")
    else:
        parts = ", ".join(f"{n} {_REC_WORDS.get(k, k.lower().replace('_', ' '))}" for k, n in ordered)
        head = f"The panel was **split**: {parts}." if parts else "The panel was **split**."
    wide = []
    for label, _, scores in parsed.dimension_rows:
        nums = [float(x) for x in scores if re.match(r"^\d+(\.\d+)?$", x)]
        if len(nums) >= 2 and max(nums) - min(nums) >= 2:
            wide.append((label, int(max(nums) - min(nums))))
    if wide:
        top_spread = max(w for _, w in wide)
        names = ", ".join(l for l, _ in wide)
        tail = f" Individual scores ranged by up to {top_spread} points on {names}."
    else:
        tail = " Individual scores agreed within a point on every dimension."
    return head + tail


def build_public_markdown(parsed: ParsedReview) -> str:
    """Render the sanitized review markdown (safe for public publication)."""
    valid_reviewers = [r for r in parsed.reviewers if not r["error"]]
    valid_n = len(valid_reviewers)
    consensus = _consensus_label(parsed)

    lines: list[str] = [
        "---",
        f'title: "Review: {parsed.title}"',
        f'doi: "{parsed.doi}"',
        f"record_id: {parsed.record_id}",
        f"review_date: {parsed.review_date}",
        f"recommendation: {parsed.recommendation}",
        f"consensus: {consensus}",
        f"reviewer_count: {valid_n}",
        "---",
        "",
        "## Open review",
        "",
        (
            f"This submission was evaluated by a panel of {valid_n} independent "
            f"advanced AI reviewers scoring six dimensions. "
            + _consensus_sentence(parsed, consensus)
        ),
        "",
        "### Aggregate scores",
        "",
        "| Dimension | Mean | Per-reviewer |",
        "|-----------|------|--------------|",
    ]
    for label, mean, scores in parsed.dimension_rows:
        lines.append(f"| {label} | {mean} | {', '.join(scores) or '—'} |")

    lines.extend([
        "",
        "### Reviewer assessments",
        "",
        (
            "Individual reviewer assessments are collapsed by default. "
            "Expand any row to read that reviewer's summary "
            "and per-dimension justification."
        ),
        "",
    ])
    # Emit raw HTML for reviewer blocks so we can use <details> for
    # collapsibility. Python-markdown passes block-level HTML through
    # unchanged, so the rendered landing page gets native browser-handled
    # expand/collapse on each reviewer without any JavaScript.
    import html as _html
    parsed.self_id_events = []
    for idx, r in enumerate(valid_reviewers, start=1):
        rec = _html.escape(r["recommendation"])
        summary_text, ev = screen_self_identification(r["summary"], f"Reviewer {idx} summary")
        parsed.self_id_events.extend(ev)
        if ev and not summary_text:
            summary_text = SELF_ID_WITHHELD
        summary = _html.escape(_rewrite_rubric_filenames(summary_text))
        lines.append(f'<details class="reviewer-detail">')
        lines.append(f'<summary><strong>Reviewer {idx}</strong> — {rec}</summary>')
        lines.append("")
        lines.append(f'<p><strong>Summary:</strong> {summary}</p>')
        if r["dimensions"]:
            lines.append("<ul>")
            for label, score, just in r["dimensions"]:
                just_text, ev = screen_self_identification(just, f"Reviewer {idx} {label}")
                parsed.self_id_events.extend(ev)
                if ev and not just_text:
                    just_text = SELF_ID_WITHHELD
                lines.append(
                    f'  <li><strong>{_html.escape(label)}</strong> '
                    f'({_html.escape(score)}): {_html.escape(_rewrite_rubric_filenames(just_text))}</li>'
                )
            lines.append("</ul>")
        lines.append("</details>")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            (
                "*Reviews at ICSAC are open and transparent. AI tooling helps "
                "the panel draft and structure each review; final acceptance "
                "decisions rest with the curation team. Reviews are published "
                "alongside acceptance for accountability; individual reviewer "
                "identities are abstracted to keep focus on the assessment "
                "rather than the tooling behind it.*"
            ),
            "",
        ]
    )
    return _humanize_internal_jargon("\n".join(lines))


def _find_substring_hits(text: str, tokens: tuple[str, ...]) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    lowered = text.lower()
    for tok in tokens:
        start = 0
        while True:
            at = lowered.find(tok, start)
            if at < 0:
                break
            hits.append((tok, at))
            start = at + 1
    return hits


def _find_wordboundary_hits(text: str, tokens: tuple[str, ...]) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for tok in tokens:
        for m in re.finditer(rf"\b{re.escape(tok)}\b", text, flags=re.IGNORECASE):
            hits.append((tok, m.start()))
    return hits


@dataclass
class ScrubReport:
    fatal_hits: list[tuple[str, int]]
    warn_hits: list[tuple[str, int]]

    @property
    def clean(self) -> bool:
        return not self.fatal_hits


class RedactionLeak(Exception):
    """Raised when a redacted artifact still contains a fatal token."""

    def __init__(self, hits: list[tuple[str, int]], artifact_path: str | None):
        self.hits = hits
        self.artifact_path = artifact_path
        preview = ", ".join(sorted({t for t, _ in hits}))
        loc = f" in {artifact_path}" if artifact_path else ""
        super().__init__(
            f"Redacted review leaked forbidden tokens{loc}: {preview} "
            f"({len(hits)} total hit(s))"
        )



def _find_regex_hits(text: str, patterns: tuple[str, ...]) -> list[tuple[str, int]]:
    """Return [(matched_text, offset), ...] for each regex pattern that matches."""
    hits: list[tuple[str, int]] = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            hits.append((f"[regex {pat!r}] {m.group(0)[:80]}", m.start()))
    return hits

_REVIEWER_DETAIL_BLOCK = re.compile(
    r'<details class="reviewer-detail">.*?</details>',
    re.DOTALL,
)


_JARGON_REWRITES = (
    (re.compile(r"\bSlots\b"), "Reviewers"),
    (re.compile(r"\bslots\b"), "reviewers"),
    (re.compile(r"\bSlot\b"), "Reviewer"),
    (re.compile(r"\bslot\b"), "reviewer"),
    # The RQC sometimes names a seat by the model that fills it ("the
    # Claude-position reviewers"); the public record names seats, never
    # vendors (2026-09-28, found on the first external paper).
    (re.compile(r"(?i)\bclaude-position\b"), "first-seat"),
    (re.compile(r"(?i)\b(?:gpt|chatgpt|openai|gemini|gemma|qwen|deepseek|llama|mistral|nemotron)-position\b"), "panel-seat"),
)


def _humanize_internal_jargon(text: str) -> str:
    """Standardize public-facing language on "reviewer" instead of "slot".

    Pipeline configuration calls each panel position a "slot"; reviewers
    occasionally echo the term when describing peer assessments. For
    public output we rewrite to "reviewer" so the report matches the
    institute's published panel description and never exposes the
    pipeline's internal naming.
    """
    for pat, repl in _JARGON_REWRITES:
        text = pat.sub(repl, text)
    return text


def _strip_reviewer_prose(text: str) -> str:
    """Remove <details class="reviewer-detail"> blocks before vendor-token
    screening.

    Reviewer-detail blocks hold per-reviewer summary + dimension
    justifications — prose that LEGITIMATELY describes what the submission
    itself says, including author disclosures of AI-assisted writing or
    references to prior-art papers (e.g. OpenAI/Gemma citations). A
    reviewer noting "the author acknowledges Claude/Gemini as writing
    assistants" is factual description, not panel-composition leak.

    Structural content — frontmatter, section headers, the aggregate
    scores table, the boilerplate footer — still receives the full vendor
    check. Secret and exfil patterns are checked across the whole text.
    """
    return _REVIEWER_DETAIL_BLOCK.sub("", text)


def scan(text: str) -> ScrubReport:
    """Return a hit report without raising."""
    structural = _strip_reviewer_prose(text)
    fatal = _find_substring_hits(structural, FORBIDDEN_VENDOR_TOKENS)
    fatal.extend(_find_substring_hits(text, FORBIDDEN_SECRET_PHRASES))
    fatal.extend(_find_regex_hits(text, FORBIDDEN_EXFIL_PATTERNS))
    fatal.extend(_find_regex_hits(text, FORBIDDEN_PANEL_PATTERNS))
    # Quotations from the paper are masked: a paper may quote a model's self-report.
    fatal.extend(_find_regex_hits(_mask_quotes(text), FORBIDDEN_SELF_ID_PATTERNS))
    warn = _find_wordboundary_hits(text, SOFT_WARN_TOKENS)
    return ScrubReport(fatal_hits=fatal, warn_hits=warn)


def assert_clean(text: str, artifact_path: str | None = None) -> ScrubReport:
    """Grep-gate: raise RedactionLeak on any fatal hit; return the scan report."""
    report = scan(text)
    if report.fatal_hits:
        raise RedactionLeak(report.fatal_hits, artifact_path)
    return report


def _strip_frontmatter(md: str) -> str:
    """Drop the YAML frontmatter from a markdown string for HTML rendering."""
    if not md.startswith("---\n"):
        return md
    end = md.find("\n---\n", 4)
    if end < 0:
        return md
    return md[end + 5 :]


def render_public_html(public_md: str) -> str:
    """Render redacted markdown into an HTML fragment for the landing page.

    Uses python-markdown with the 'tables' extension; falls back to a
    preformatted-text dump if markdown is not importable.
    """
    body = _strip_frontmatter(public_md)
    try:
        import markdown as _md

        return _md.markdown(body, extensions=["tables"])
    except ImportError:
        import html as _html

        return f"<pre>{_html.escape(body)}</pre>"


def publish_public_review(
    record_id: str,
    reviews_dir: str,
    website_repo: str,
) -> str:
    """Redact reviews/<id>_*.md and write md + html to website repo public-reviews/.

    Returns the written .md path. Raises RedactionLeak if grep-gate trips on
    either the markdown source or the rendered HTML.
    """
    # Exclude artifacts that are not the primary review markdown.
    # Both RQC and citation reports live alongside in reviews/ as
    # <id>_review_quality_control.md and <id>_citations.md; including
    # them here would cause sorted()[-1] to silently pick the wrong
    # source for any paper whose slug sorts before "review_quality_control".
    _NON_REVIEW_SUFFIXES = ("_review_quality_control.md", "_citations.md")
    matches = [
        f for f in os.listdir(reviews_dir)
        if f.startswith(f"{record_id}_") and f.endswith(".md")
        and not any(f.endswith(s) for s in _NON_REVIEW_SUFFIXES)
    ]
    if not matches:
        raise FileNotFoundError(
            f"No review markdown found for record_id={record_id} in {reviews_dir}"
        )
    src = os.path.join(reviews_dir, sorted(matches)[-1])
    parsed = parse_review_file(src)
    public_md = build_public_markdown(parsed)
    report_md = assert_clean(public_md, artifact_path=src)
    if parsed.self_id_events:
        print("  redaction: self-identification screened in " + src + ": "
              + format_self_id_events(parsed.self_id_events).splitlines()[0])

    public_html = render_public_html(public_md)
    assert_clean(public_html, artifact_path=f"{src} (rendered html)")

    out_dir = os.path.join(website_repo, "src", "data", "public-reviews")
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"{record_id}.md")
    html_path = os.path.join(out_dir, f"{record_id}.html")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(public_md)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(public_html)
    if report_md.warn_hits:
        uniq = sorted({t for t, _ in report_md.warn_hits})
        print(
            f"  redaction: {len(report_md.warn_hits)} soft-warn hit(s) "
            f"({', '.join(uniq)}) — inspect {md_path} if unexpected."
        )
    return md_path


# --------------------------------------------------------------------------
# Review Quality Control — public redaction
# --------------------------------------------------------------------------

# The RQC rubric defines six dimensions. The first five are publishable;
# ``injection_indicators`` is INTERNAL ONLY and must never appear in any
# file written under src/data/public-reviews/. See rubrics/review_quality_control.md.
# "Evidence Use" (2026-10-01) is absent from records audited before that date.
RQC_PUBLIC_DIMENSIONS: tuple[str, ...] = (
    "Rubric Adherence",
    "Internal Consistency",
    "Specificity",
    "Tone",
    "Evidence Use",
)
RQC_INJECTION_DIM_LABEL = "Injection Indicators"

# Substring-guard against the redacted dimension leaking into public text.
# Match case-insensitively on the prose label AND any plausible variants.
RQC_FORBIDDEN_PUBLIC_TOKENS: tuple[str, ...] = (
    "injection_indicators",
    "injection indicators",
    "injection indicator",
    "prompt injection",
    "prompt-injection",
    "prompt_injection",
)


@dataclass
class ParsedRQC:
    """Structured view of a reviews/<id>_review_quality_control.md file."""

    record_id: str
    title: str
    doi: str
    audit_date: str
    flag: bool
    summary: str
    overall_concerns: list[str] = field(default_factory=list)
    # Each slot: {"reviewer": str, "errored": bool,
    #             "dimensions": [(label, score, justification), ...]}
    slots: list[dict] = field(default_factory=list)
    # Filled by build_public_rqc_markdown: what the self-identification screen did.
    self_id_events: list[dict] = field(default_factory=list)


def _parse_rqc_slots(body: str) -> list[dict]:
    """Parse '### Reviewer N' sections out of the Per-slot audit block."""
    marker = "\n## Per-slot audit\n"
    idx = body.find(marker)
    if idx < 0:
        return []
    remainder = body[idx + len(marker):]
    end = remainder.find("\n---\n")
    if end >= 0:
        remainder = remainder[:end]

    sections: list[tuple[str, str]] = []
    current_head: str | None = None
    current_lines: list[str] = []
    for line in remainder.splitlines():
        if line.startswith("### "):
            if current_head is not None:
                sections.append((current_head, "\n".join(current_lines).strip()))
            current_head = line[4:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_head is not None:
        sections.append((current_head, "\n".join(current_lines).strip()))

    slots: list[dict] = []
    for head, content in sections:
        errored = content.lstrip().startswith("*Errored:")
        dims: list[tuple[str, str, str]] = []
        if not errored:
            for m in re.finditer(
                r"^-\s+\*\*(?P<label>[^*]+)\*\*\s*\((?P<score>[^)]+)\):\s*"
                r"(?P<just>.+?)(?=\n-\s+\*\*|\Z)",
                content,
                re.S | re.M,
            ):
                dims.append(
                    (
                        m.group("label").strip(),
                        m.group("score").strip(),
                        " ".join(m.group("just").split()),
                    )
                )
        slots.append({"reviewer": head, "errored": errored, "dimensions": dims})
    return slots


def parse_rqc_file(path: str) -> ParsedRQC:
    """Load a reviews/<id>_review_quality_control.md file into structured form."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    fm, body = _parse_frontmatter(text)
    title = fm.get("title", "").strip('"')
    if title.lower().startswith("review quality control:"):
        title = title.split(":", 1)[1].strip()

    flag_raw = str(fm.get("review_quality_control_flag", "false")).lower()
    flag = flag_raw == "true"

    # Pull the summary paragraph between "## Summary" and the next "## " header
    summary = ""
    m = re.search(r"^##\s+Summary\s*\n(.+?)(?=^##\s+|\Z)", body, re.S | re.M)
    if m:
        summary = " ".join(m.group(1).split())

    concerns: list[str] = []
    m = re.search(r"^##\s+Overall concerns\s*\n(.+?)(?=^##\s+|\Z)", body, re.S | re.M)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                concerns.append(line[2:].strip())

    return ParsedRQC(
        record_id=str(fm.get("record_id", "")),
        title=title,
        doi=fm.get("doi", ""),
        audit_date=fm.get("audit_date", ""),
        flag=flag,
        summary=summary,
        overall_concerns=concerns,
        slots=_parse_rqc_slots(body),
    )


def _flag_cause_is_injection_only(parsed: ParsedRQC) -> bool:
    """True iff flag=true AND no scholarly dimension scored <=2.

    When the only trigger is the internal injection_indicators dimension,
    the public rendering shows a generic operator-review note rather than
    identifying which scholarly dim tripped it.
    """
    if not parsed.flag:
        return False
    for slot in parsed.slots:
        if slot["errored"]:
            continue
        for label, score, _ in slot["dimensions"]:
            if label == RQC_INJECTION_DIM_LABEL:
                continue
            try:
                n = int(re.match(r"(\d+)", score).group(1))
            except Exception:
                continue
            if n <= 2:
                return False
    return True


def build_public_rqc_markdown(parsed: ParsedRQC) -> str:
    """Render the redacted RQC markdown. Omits injection_indicators entirely.

    Contract: the returned string contains no reference to the
    injection_indicators dimension under any spelling. ``assert_rqc_clean``
    enforces this before the redaction writes anything to the site.
    """
    parsed.self_id_events = []
    status_line = (
        "Review Quality Control: flagged — reviewed by the curation team before acceptance."
        if parsed.flag
        else "Review Quality Control: passed."
    )

    lines: list[str] = [
        "---",
        f'title: "Review Quality Control: {parsed.title}"',
        f'doi: "{parsed.doi}"',
        f"record_id: {parsed.record_id}",
        f"audit_date: {parsed.audit_date}",
        f"review_quality_control_flag: {str(parsed.flag).lower()}",
        "---",
        "",
        "## Review Quality Control",
        "",
        f"**{status_line}**",
        "",
        (
            "This audit quality checks each AI reviewer's assessment for "
            "rubric adherence, internal consistency, specificity, "
            "institutional voice and use of the verification evidence the "
            "panel was given (the citation checks and the code package "
            "digest). It is published alongside the panel review "
            "so the quality of the review process is as auditable as the "
            "review itself."
        ),
        "",
    ]

    # When flag was tripped only by the internal dimension, say so generically.
    if parsed.flag and _flag_cause_is_injection_only(parsed):
        lines.extend([
            (
                "The audit surfaced a concern outside the scholarly "
                "dimensions above. The curation team reviewed the panel output "
                "before the acceptance decision was recorded."
            ),
            "",
        ])
    elif parsed.overall_concerns:
        # Concerns may reference the redacted injection dimension, name
        # specific reviewer numbers that won't match the public 1..N
        # renumbering (we drop errored slots from public view), or call
        # out pipeline-level mechanics that are operator noise rather than
        # author-facing scholarly concerns. Filter aggressively.
        operator_noise_patterns = (
            "pipeline-level", "pipeline level", "slot-run", "slot run",
            "errored slot", "errored slots", "slot errored", "reviewer defect",
            "operator", "panel composition", "pass 1", "pass 2", "pass 3",
        )
        safe_concerns = []
        for c in parsed.overall_concerns:
            cl = c.lower()
            if any(tok in cl for tok in RQC_FORBIDDEN_PUBLIC_TOKENS):
                continue
            if any(tok in cl for tok in operator_noise_patterns):
                continue
            # Concerns that name specific panel members by vendor identity
            # are panel-composition leaks, not author-facing scholarly
            # concerns. Drop them rather than try to rewrite.
            if any(tok in cl for tok in FORBIDDEN_VENDOR_TOKENS):
                continue
            # Drop concerns that cite specific reviewer numbers — our public
            # renumbering (valid-only 1..N) won't line up with whatever
            # index the audit claude-p referenced in its internal output.
            # Singular AND plural forms both count.
            if re.search(r"\breviewers?\s+\d+\b", cl):
                continue
            c, ev = screen_self_identification(c, "RQC note")
            parsed.self_id_events.extend(ev)
            if not c:
                continue
            safe_concerns.append(c)
        if safe_concerns:
            lines.extend(["### Notes", ""])
            for c in safe_concerns:
                lines.append(f"- {_rewrite_rubric_filenames(c)}")
            lines.append("")

    # Public audit shows only valid-slot outputs, renumbered 1..N to match
    # the open review's reviewer count. Errored slots are operator-layer
    # signal; surfacing them publicly creates a count mismatch with the
    # review section and invites reader confusion.
    valid_slots = [s for s in parsed.slots if not s["errored"]]

    lines.extend(["### Reviewer Quality Control Audit", ""])

    # Condensed table: reviewer × four scholarly dimensions.
    lines.append(
        "| Reviewer | " + " | ".join(RQC_PUBLIC_DIMENSIONS) + " |"
    )
    lines.append(
        "|----------|" + "|".join(["----"] * len(RQC_PUBLIC_DIMENSIONS)) + "|"
    )
    for idx, slot in enumerate(valid_slots, start=1):
        label = f"Reviewer {idx}"
        by_label = {lbl: (score, just) for lbl, score, just in slot["dimensions"]}
        cells = [label]
        for dim in RQC_PUBLIC_DIMENSIONS:
            score, _ = by_label.get(dim, ("—", ""))
            cells.append(score)
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    # Detail block per valid reviewer, collapsed by default. Scholarly dims
    # only; injection_indicators is redacted upstream. Browser-native
    # <details> means no JavaScript and works with the site's scoped CSS.
    import html as _html
    for idx, slot in enumerate(valid_slots, start=1):
        label = f"Reviewer {idx}"
        by_label = {lbl: (score, just) for lbl, score, just in slot["dimensions"]}
        lines.append(f'<details class="reviewer-detail">')
        lines.append(f'<summary><strong>{label}</strong></summary>')
        lines.append("")
        lines.append("<ul>")
        for dim in RQC_PUBLIC_DIMENSIONS:
            score, just = by_label.get(dim, ("—", ""))
            if just:
                just, ev = screen_self_identification(just, f"RQC {label} {dim}")
                parsed.self_id_events.extend(ev)
                if ev and not just:
                    just = SELF_ID_WITHHELD
            just_clean = _rewrite_rubric_filenames(just) if just else "No justification recorded."
            lines.append(
                f'  <li><strong>{_html.escape(dim)}</strong> '
                f'({_html.escape(score)}): {_html.escape(just_clean)}</li>'
            )
        lines.append("</ul>")
        lines.append("</details>")
        lines.append("")

    lines.extend([
        "---",
        "",
        (
            "*Review Quality Control is an internal ICSAC audit of the "
            "panel review itself. The four dimensions above are published "
            "as part of ICSAC's open review commitment.*"
        ),
        "",
    ])
    return _humanize_internal_jargon("\n".join(lines))


def assert_rqc_clean(text: str, artifact_path: str | None = None) -> ScrubReport:
    """Grep-gate for RQC public output.

    Runs the standard fatal/warn gate (vendors, secrets, exfil patterns)
    AND asserts that no reference to the redacted injection_indicators
    dimension survives. A leak is treated as a RedactionLeak.
    """
    report = scan(text)
    lowered = text.lower()
    extra_hits: list[tuple[str, int]] = []
    for tok in RQC_FORBIDDEN_PUBLIC_TOKENS:
        start = 0
        while True:
            at = lowered.find(tok, start)
            if at < 0:
                break
            extra_hits.append((f"[rqc-redacted] {tok}", at))
            start = at + 1
    if extra_hits or report.fatal_hits:
        raise RedactionLeak(report.fatal_hits + extra_hits, artifact_path)
    return report


def publish_public_rqc(
    record_id: str,
    reviews_dir: str,
    website_repo: str,
) -> str | None:
    """Redact reviews/<id>_review_quality_control.md → public-reviews/<id>_review_quality_control.{md,html}.

    Returns the written .md path, or None if no RQC file exists for the
    record. Raises RedactionLeak if any forbidden token (including references
    to the redacted injection_indicators dimension) survives.
    """
    src = os.path.join(reviews_dir, f"{record_id}_review_quality_control.md")
    if not os.path.isfile(src):
        return None
    parsed = parse_rqc_file(src)
    public_md = build_public_rqc_markdown(parsed)
    report_md = assert_rqc_clean(public_md, artifact_path=src)
    if parsed.self_id_events:
        print("  redaction/rqc: self-identification screened in " + src + ": "
              + format_self_id_events(parsed.self_id_events).splitlines()[0])

    public_html = render_public_html(public_md)
    assert_rqc_clean(public_html, artifact_path=f"{src} (rendered html)")

    out_dir = os.path.join(website_repo, "src", "data", "public-reviews")
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"{record_id}_review_quality_control.md")
    html_path = os.path.join(out_dir, f"{record_id}_review_quality_control.html")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(public_md)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(public_html)
    if report_md.warn_hits:
        uniq = sorted({t for t, _ in report_md.warn_hits})
        print(
            f"  redaction/rqc: {len(report_md.warn_hits)} soft-warn hit(s) "
            f"({', '.join(uniq)}) — inspect {md_path} if unexpected."
        )
    return md_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(
            "usage: python3 redaction.py <record_id> [reviews_dir] [website_repo]\n"
            "       python3 redaction.py rqc <record_id> [reviews_dir] [website_repo]",
            file=sys.stderr,
        )
        sys.exit(2)

    if sys.argv[1] == "rqc":
        if len(sys.argv) < 3:
            print("usage: python3 redaction.py rqc <record_id> ...", file=sys.stderr)
            sys.exit(2)
        record = sys.argv[2]
        rdir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.path.dirname(__file__), "reviews")
        wrepo = (
            sys.argv[4]
            if len(sys.argv) > 4
            else os.path.expanduser("~/Desktop/icsac/icsacinstitute.org")
        )
        try:
            written = publish_public_rqc(record, rdir, wrepo)
        except RedactionLeak as e:
            print(f"SCRUB LEAK: {e}", file=sys.stderr)
            sys.exit(1)
        if written is None:
            print(f"No RQC file for record {record} — nothing to publish.")
            sys.exit(0)
        print(f"wrote {written}")
        sys.exit(0)

    record = sys.argv[1]
    rdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "reviews")
    wrepo = (
        sys.argv[3]
        if len(sys.argv) > 3
        else os.path.expanduser("~/Desktop/icsac/icsacinstitute.org")
    )
    try:
        written = publish_public_review(record, rdir, wrepo)
    except RedactionLeak as e:
        print(f"SCRUB LEAK: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"wrote {written}")

    # Best-effort companion RQC publish.
    try:
        rqc_written = publish_public_rqc(record, rdir, wrepo)
    except RedactionLeak as e:
        print(f"SCRUB LEAK (rqc): {e}", file=sys.stderr)
        sys.exit(1)
    if rqc_written:
        print(f"wrote {rqc_written}")
