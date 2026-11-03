"""Panel-quality snapshot.

Reads every reviews/<id>_*.md and emits a JSON snapshot the website can
render at /stats. Durability > volume: the dashboard is insurance against
the panel silently rubber-stamping as traffic scales.

Metrics:
  - Total reviewed (all time) and within rolling 30-day window
  - Recommendation mix (RECOMMEND / REVIEW_FURTHER / REVISE_AND_RESUBMIT / REJECT / PAUSED_AI_FAILURE)
  - Disagreement rate (fraction where reviewers split verdicts)
  - Per-dimension mean-of-means distribution (histogram bins)
  - AI provenance-flag rate (fraction where ai_provenance_signal mean ≤ 2)
  - Curation-team verdicts, one per submission round (accept / revise / scope_reject)

No model/vendor identities leak — this snapshot is safe to publish.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections import Counter


RECOMMENDATIONS = ("RECOMMEND", "REVIEW_FURTHER", "REVISE_AND_RESUBMIT", "REJECT", "PAUSED_AI_FAILURE")
# The curation team's verdicts as state.json records them.
VERDICTS = ("accept", "revise", "scope_reject")
DIMENSIONS = (
    "Domain Fit",
    "Methodological Transparency",
    "Internal Consistency",
    "Citation Integrity",
    "Novelty Signal",
    "AI Provenance Signal",
)


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    out: dict = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# The raw review writes "## Aggregate Scores"; the published copy writes
# "### Aggregate scores". Reviews before 2026-05-17 name the last dimension
# by its old label.
_AGG_HEADING = re.compile(r"^#{2,3}\s+aggregate\s+scores\b", re.I)
_LABEL_ALIASES = {"AI Slop Detection": "AI Provenance Signal"}


def _parse_aggregate_means(text: str) -> dict[str, float]:
    """Pull dimension → mean from the aggregate markdown table."""
    means: dict[str, float] = {}
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if _AGG_HEADING.match(stripped):
            in_table = True
            continue
        if in_table and stripped.startswith("#"):
            break
        if not in_table or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0].lower() == "dimension":
            continue
        if set("".join(cells)) <= set("- "):
            continue
        label = _LABEL_ALIASES.get(cells[0], cells[0])
        try:
            means[label] = float(cells[1])
        except ValueError:
            continue
    return means


def _parse_review_date(raw: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_rqc_flags(reviews_dir: str) -> dict[str, bool]:
    """Parse review_quality_control_flag from every RQC file by record_id.

    Returns {record_id: bool}. Missing RQC file for a record yields no key;
    the flag-rate metric excludes un-audited records from both numerator
    and denominator.
    """
    flags: dict[str, bool] = {}
    if not os.path.isdir(reviews_dir):
        return flags
    for name in sorted(os.listdir(reviews_dir)):
        if not name.endswith("_review_quality_control.md"):
            continue
        path = os.path.join(reviews_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            fm = _parse_frontmatter(f.read())
        rid = str(fm.get("record_id", name.split("_", 1)[0]))
        raw = str(fm.get("review_quality_control_flag", "false")).lower()
        flags[rid] = raw == "true"
    return flags


def _load_reviews(reviews_dir: str) -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(reviews_dir):
        return out
    # Defense-in-depth guard: stats are computed off the per-paper review
    # markdown files in reviews/, NOT off the audit log. Test submissions
    # bail at the intake handler before the panel runs and therefore
    # never produce <id>_*.md files in this directory. The guard below
    # asserts the caller did not accidentally hand us a glob that pulls
    # in audit-log-test.jsonl alongside the markdown set; if anyone ever
    # rewires _load_reviews, this trips before contamination can happen.
    assert "test" not in os.path.basename(reviews_dir.rstrip("/")), (
        f"stats.py refuses to read from a directory whose basename "
        f"contains 'test': {reviews_dir!r}"
    )
    rqc_flags = _load_rqc_flags(reviews_dir)
    for name in sorted(os.listdir(reviews_dir)):
        if not name.endswith(".md"):
            continue
        if name.endswith("_review_quality_control.md"):
            # Folded in via rqc_flags; not a panel review.
            continue
        if name.endswith("_citations.md"):
            # Pre-review citation verification artifact, not a panel review.
            continue
        if name.endswith("_code_digest.md"):
            # Digest of the author's code package, not a panel review.
            continue
        if "ICSAC-SUB-TEST-" in name:
            # Belt-and-suspenders: if a test review file ever does end up
            # in reviews/ (e.g. from a hand-run experiment), skip it so
            # public stats never count test data.
            continue
        path = os.path.join(reviews_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        fm = _parse_frontmatter(text)
        if not fm:
            # A panel review always opens with its frontmatter; other markdown
            # beside it is not a review and must not count as one.
            continue
        means = _parse_aggregate_means(text)
        rid = str(fm.get("record_id", name.split("_", 1)[0]))
        out.append(
            {
                "record_id": rid,
                "recommendation": fm.get("recommendation", "REVIEW_FURTHER"),
                # Published copies carry "consensus: divided" instead.
                "disagreement": (fm["disagreement"].lower() == "true") if "disagreement" in fm
                                else fm.get("consensus", "").lower() in ("divided", "split"),
                "review_date": _parse_review_date(fm.get("review_date", "")),
                "dimension_means": means,
                "rqc_flag": rqc_flags.get(rid),
                # A revised version names the submission it supersedes (2026-10-01);
                # unique manuscripts = reviews that are not revisions of another.
                "revision_of": fm.get("revision_of") or None,
            }
        )
    return out


def _histogram(values: list[float]) -> dict[str, int]:
    """Distribute 1.0–5.0 scores into five 1-wide bins."""
    bins = {"1-1.99": 0, "2-2.99": 0, "3-3.99": 0, "4-4.99": 0, "5": 0}
    for v in values:
        if v >= 5:
            bins["5"] += 1
        elif v >= 4:
            bins["4-4.99"] += 1
        elif v >= 3:
            bins["3-3.99"] += 1
        elif v >= 2:
            bins["2-2.99"] += 1
        else:
            bins["1-1.99"] += 1
    return bins


def _default_submissions_root() -> str:
    return os.environ.get("ICSAC_SUBMISSIONS_ROOT", os.path.expanduser("~/icsac-submissions"))


def _norm_doi(raw: object) -> str:
    d = str(raw or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d


class VerdictsUnreadable(Exception):
    """A submission's state.json exists but cannot be read."""


def _load_verdicts(submissions_dir: str | None, public_dir: str | None) -> dict[str, int] | None:
    """The curation team's verdicts, one per submission round.

    Each submission folder records its verdict in state.json. A revised paper
    arrives under a new submission number, so every round counts once. Papers
    accepted before the submission form existed have no folder: their published
    review copy is the record (only accepted papers publish one), and a copy
    whose DOI a submission folder already carries counts once, under the folder.
    Returns None when the folder is missing or a state.json cannot be read, so
    the page shows no table rather than a count that silently leaves rows out.
    """
    if not submissions_dir or not os.path.isdir(submissions_dir):
        return None
    counts: Counter = Counter()
    sub_dois: set[str] = set()
    try:
        for name in sorted(os.listdir(submissions_dir)):
            if not re.fullmatch(r"ICSAC-SUB-\d{5}", name):
                continue   # test submissions, queues, the publish hand-off folder
            folder = os.path.join(submissions_dir, name)
            sub_path = os.path.join(folder, "submission.json")
            if os.path.exists(sub_path):
                with open(sub_path, encoding="utf-8") as f:
                    sub = json.load(f)
                for key in ("doi", "external_doi"):
                    if sub.get(key):
                        sub_dois.add(_norm_doi(sub[key]))
            state_path = os.path.join(folder, "state.json")
            if not os.path.exists(state_path):
                continue   # not decided yet
            with open(state_path, encoding="utf-8") as f:
                decision = json.load(f).get("decision")
            if decision:
                counts[str(decision)] += 1
    except (OSError, ValueError) as exc:
        raise VerdictsUnreadable(str(exc)) from exc
    if public_dir and os.path.isdir(public_dir):
        seen: set[str] = set()
        for name in sorted(os.listdir(public_dir)):
            if not name.endswith(".md") or name.endswith(
                    ("_citations.md", "_review_quality_control.md", "_code_digest.md")):
                continue
            with open(os.path.join(public_dir, name), encoding="utf-8") as f:
                fm = _parse_frontmatter(f.read())
            if not fm:
                continue
            rid = str(fm.get("record_id", ""))
            if rid.startswith("ICSAC-SUB-"):
                continue   # counted from its submission folder
            key = _norm_doi(fm.get("doi")) or rid
            if not key or key in sub_dois or key in seen:
                continue
            seen.add(key)
            counts["accept"] += 1
    out = {v: counts.get(v, 0) for v in VERDICTS}
    other = sum(n for v, n in counts.items() if v not in VERDICTS)
    if other:
        out["other"] = other
    return out


def compute_stats(reviews_dir: str, public_dir: str | None = None,
                  submissions_dir: str | None = None) -> dict:
    """public_dir: the website's published review records. The raw files behind
    the papers reviewed before the public flip (2026-05-16) are gone from
    reviews/, so the published copies are the only record of those reviews;
    a record in both places counts once (raw wins).
    submissions_dir: the intake's submission folders; given, the snapshot also
    carries the curation team's verdicts (see _load_verdicts)."""
    reviews = _load_reviews(reviews_dir)
    if public_dir:
        seen = {r["record_id"] for r in reviews}
        reviews += [r for r in _load_reviews(public_dir) if r["record_id"] not in seen]
    try:
        verdicts = _load_verdicts(submissions_dir, public_dir)
    except VerdictsUnreadable:
        verdicts = None
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=30)

    window = [r for r in reviews if r["review_date"] and r["review_date"] >= cutoff]

    rec_counts = Counter(r["recommendation"] for r in reviews)
    rec_counts_30d = Counter(r["recommendation"] for r in window)

    disagree_30d = sum(1 for r in window if r["disagreement"])

    dim_hist: dict[str, dict[str, int]] = {}
    dim_means: dict[str, float] = {}
    for dim in DIMENSIONS:
        vals = [r["dimension_means"][dim] for r in reviews if dim in r["dimension_means"]]
        dim_hist[dim] = _histogram(vals)
        dim_means[dim] = round(sum(vals) / len(vals), 2) if vals else 0.0

    provenance_hits = sum(
        1 for r in reviews if r["dimension_means"].get("AI Provenance Signal", 5) <= 2
    )

    total = len(reviews)
    total_30d = len(window)
    # One paper revised twice is three reviews and one manuscript: the
    # denominator the stats page labels as unique manuscripts.
    unique = sum(1 for r in reviews if not r.get("revision_of"))
    unique_30d = sum(1 for r in window if not r.get("revision_of"))

    # RQC flag-rate: only count records that were actually audited.
    # A None rqc_flag means RQC did not run (older reviews pre-rollout).
    audited = [r for r in reviews if r.get("rqc_flag") is not None]
    audited_30d = [r for r in window if r.get("rqc_flag") is not None]
    rqc_flagged = sum(1 for r in audited if r["rqc_flag"])
    rqc_flagged_30d = sum(1 for r in audited_30d if r["rqc_flag"])

    def _rate(num: int, denom: int) -> float:
        return round(num / denom, 3) if denom else 0.0

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_reviewed": total,
        "total_reviewed_30d": total_30d,
        "unique_manuscripts": unique,
        "unique_manuscripts_30d": unique_30d,
        "revision_reviews": total - unique,
        "recommendation_mix": {r: rec_counts.get(r, 0) for r in RECOMMENDATIONS},
        "recommendation_mix_30d": {r: rec_counts_30d.get(r, 0) for r in RECOMMENDATIONS},
        "reject_rate_30d": _rate(rec_counts_30d.get("REJECT", 0), total_30d),
        "recommend_rate_30d": _rate(rec_counts_30d.get("RECOMMEND", 0), total_30d),
        "disagreement_rate_30d": _rate(disagree_30d, total_30d),
        "provenance_hit_rate_overall": _rate(provenance_hits, total),
        "dimension_means_overall": dim_means,
        "dimension_distribution_overall": dim_hist,
        "rqc_audited_count": len(audited),
        "rqc_audited_count_30d": len(audited_30d),
        "rqc_flagged_count_30d": rqc_flagged_30d,
        "rqc_flag_rate_overall": _rate(rqc_flagged, len(audited)),
        "rqc_flag_rate_30d": _rate(rqc_flagged_30d, len(audited_30d)),
        **({"curation_verdicts": verdicts} if verdicts is not None else {}),
    }


def write_stats(reviews_dir: str, out_path: str, public_dir: str | None = None,
                submissions_dir: str | None = None) -> str:
    """submissions_dir defaults to the intake's folder ($ICSAC_SUBMISSIONS_ROOT
    or ~/icsac-submissions), so every writer carries the verdicts."""
    if submissions_dir is None:
        submissions_dir = _default_submissions_root()
    stats = compute_stats(reviews_dir, public_dir, submissions_dir)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return out_path


if __name__ == "__main__":
    import sys

    rdir = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviews")
    )
    out = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.path.expanduser(
            "~/Desktop/icsac/icsacinstitute.org/src/data/stats.json"
        )
    )
    pub = (
        sys.argv[3]
        if len(sys.argv) > 3
        else os.path.join(os.path.dirname(os.path.dirname(out)), "data", "public-reviews")
    )
    subs = sys.argv[4] if len(sys.argv) > 4 else None
    written = write_stats(rdir, out, pub if os.path.isdir(pub) else None, subs)
    print(f"wrote {written}")
