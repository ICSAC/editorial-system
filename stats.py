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

No model/vendor identities leak — this snapshot is safe to publish.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections import Counter


RECOMMENDATIONS = ("RECOMMEND", "REVIEW_FURTHER", "REVISE_AND_RESUBMIT", "REJECT", "PAUSED_AI_FAILURE")
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


def _parse_aggregate_means(text: str) -> dict[str, float]:
    """Pull dimension → mean from the aggregate markdown table."""
    means: dict[str, float] = {}
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Aggregate Scores"):
            in_table = True
            continue
        if in_table and stripped.startswith("## "):
            break
        if not in_table or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0].lower() == "dimension":
            continue
        if set("".join(cells)) <= set("- "):
            continue
        label = cells[0]
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
        if "ICSAC-SUB-TEST-" in name:
            # Belt-and-suspenders: if a test review file ever does end up
            # in reviews/ (e.g. from a hand-run experiment), skip it so
            # public stats never count test data.
            continue
        path = os.path.join(reviews_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        fm = _parse_frontmatter(text)
        means = _parse_aggregate_means(text)
        rid = str(fm.get("record_id", name.split("_", 1)[0]))
        out.append(
            {
                "record_id": rid,
                "recommendation": fm.get("recommendation", "REVIEW_FURTHER"),
                "disagreement": fm.get("disagreement", "False").lower() == "true",
                "review_date": _parse_review_date(fm.get("review_date", "")),
                "dimension_means": means,
                "rqc_flag": rqc_flags.get(rid),
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


def compute_stats(reviews_dir: str) -> dict:
    reviews = _load_reviews(reviews_dir)
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
    }


def write_stats(reviews_dir: str, out_path: str) -> str:
    stats = compute_stats(reviews_dir)
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
    written = write_stats(rdir, out)
    print(f"wrote {written}")
