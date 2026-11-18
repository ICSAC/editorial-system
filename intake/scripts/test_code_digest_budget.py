#!/usr/bin/env python3
"""Code package digest: the code is never cut by the size cap. Offline.

An archive with a couple of hundred data files under long paths filled the
whole digest with its file listing, and the size cap then removed the README,
the definitions and the construct hits: the panel saw a list of names and no
code. The digest now writes the code first and the listing last, grouped by
folder and type, and only the listing is cut.

    .venv/bin/python intake/scripts/test_code_digest_budget.py
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import code_digest as cdg  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def package(n_data: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tidepool-survey-archive/README.md",
                    "# Tide pool survey archive\nRun analysis/estimate_rpi.py to rebuild every table.\n")
        zf.writestr("tidepool-survey-archive/analysis/estimate_rpi.py",
                    "import json\n\ndef load_counts(path):\n    return json.load(open(path))\n\n"
                    "def estimate_rpi(counts, area):\n    RPI = counts / area\n    return RPI\n")
        zf.writestr("tidepool-survey-archive/analysis/plot_tables.py",
                    "def plot_rpi_table(rows):\n    pass\n")
        for i in range(n_data):
            zf.writestr(f"tidepool-survey-archive/data/raw_quadrat_counts_by_site/site_{i:03d}_"
                        f"quadrat_counts_with_replicate_metadata_v2.json", "{\"n\": %d}\n" % i)
        for i in range(12):
            zf.writestr(f"tidepool-survey-archive/figures/figure_{i:02d}_rpi_by_site.png", b"\x89PNG\r\n")
    return buf.getvalue()


print("[1] a 220-file package keeps its code in the digest")
data = package(205)
rows = cdg.expand([{"name": "archive.zip", "size": len(data), "data": data}])
check(len(rows) >= 220, f"{len(rows)} files in the package")
text = "The RPI index ... RPI per quadrat ... RPI by site ... the RPI table"
terms = cdg.constructs_from_text(text)
md = cdg.render("Zenodo record 1 (test)", "Record title: t", rows, terms)
check(len(md) <= cdg.MAX_DIGEST_CHARS, f"digest within the cap ({len(md)} <= {cdg.MAX_DIGEST_CHARS})")
check("Definitions per source file:" in md, "the definitions section is present")
check("estimate_rpi.py: load_counts, estimate_rpi" in md, "the definitions name the functions")
check("estimate_rpi.py:7: RPI = counts / area" in md, "a construct hit with file:line is present")
check("README head" in md and "Run analysis/estimate_rpi.py" in md, "the README head is present")
check("[digest truncated at the size cap]" not in md, "the code was not truncated")
check("data/raw_quadrat_counts_by_site/*.json  (205 files" in md, "the data folder is one line with its count")
check("figures/*.png  (12 files" in md, "a second data folder is one line too")
check("analysis/estimate_rpi.py  (" in md and "analysis/plot_tables.py  (" in md,
      "source files stay listed by name")
check("Definitions per source file:" in md and "Files (" in md
      and md.index("Definitions per source file:") < md.index("Files ("), "the listing comes after the code")

print("[2] a small cap cuts the listing, never the code")
old_cap = cdg.MAX_DIGEST_CHARS
try:
    cdg.MAX_DIGEST_CHARS = len(md) - 200
    md2 = cdg.render("Zenodo record 1 (test)", "Record title: t", rows, terms)
    check(len(md2) <= cdg.MAX_DIGEST_CHARS, f"digest within the smaller cap ({len(md2)})")
    check("estimate_rpi.py:7: RPI = counts / area" in md2, "the construct hit survives a small cap")
    check("not shown (size cap)" in md2 or "listing cut" in md2, "the listing says it was cut")
finally:
    cdg.MAX_DIGEST_CHARS = old_cap

print("[2b] a very small configured cap still keeps a construct hit and stays within the cap")
old_cap = cdg.MAX_DIGEST_CHARS
try:
    cdg.MAX_DIGEST_CHARS = 3000
    md3 = cdg.render("Zenodo record 1 (test)", "Record title: t", rows, terms)
    check(len(md3) <= 3000, f"within a 3,000-char cap ({len(md3)})")
    check("estimate_rpi.py:7: RPI = counts / area" in md3, "the construct hit survives it")
finally:
    cdg.MAX_DIGEST_CHARS = old_cap

print("[2c] a small package keeps every file name")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("pkg/run.py", "def run():\n    RPI = 1\n")
    for i in range(5):
        zf.writestr(f"pkg/data/site_{i}.csv", "a,b\n1,2\n")
small = cdg.expand([{"name": "s.zip", "size": buf.tell(), "data": buf.getvalue()}])
md4 = cdg.render("Zenodo record 2 (test)", "Record title: t", small, terms)
check(all(f"pkg/data/site_{i}.csv" in md4 for i in range(5)), "five data files are listed by name, not grouped")

print("[3] the RQC evidence block still finds the code sections")
import tempfile  # noqa: E402
import review_quality_control as rqc  # noqa: E402
with tempfile.TemporaryDirectory(prefix="icsac-digest-") as td:
    (Path(td) / "ICSAC-SUB-TEST-9_code_digest.md").write_text("# Code package digest\n\n```\n" + md + "\n```\n")
    ev = rqc.build_evidence_block("ICSAC-SUB-TEST-9", td)
    check("estimate_rpi.py:7: RPI = counts / area" in ev and "estimate_rpi.py: load_counts" in ev,
          "definitions and construct hits reach the RQC")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
