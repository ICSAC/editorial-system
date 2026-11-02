#!/usr/bin/env python3
"""A name inside an f-string that nothing in its module binds is a NameError waiting
for that line to run. Log and error lines run rarely, so the suites never reach them
(a stats log line read fresh.get(total_reviewed), the key's quotes lost). This scans
every module of the repo statically. Run from the repo root:
    .venv/bin/python intake/scripts/test_fstring_names.py
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKIP = {".venv", "node_modules", ".git"}


def unbound_fstring_names(source: str, filename: str = "<src>") -> list[tuple[int, str]]:
    tree = ast.parse(source, filename)
    bound = set(dir(builtins))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            bound.add(n.name)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for m in ast.walk(n):
                if isinstance(m, ast.Name) and isinstance(m.ctx, ast.Load) and m.id not in bound:
                    out.append((m.lineno, m.id))
    return out


def main() -> int:
    failures = []
    # the check must bite: the original stats line, and a clean twin
    bad = 'def f(fresh, published):\n    print(f"a fresh count of {fresh.get(total_reviewed)} vs {published}")\n'
    good = bad.replace("get(total_reviewed)", "get(\'total_reviewed\')")
    if unbound_fstring_names(bad) != [(2, "total_reviewed")]:
        failures.append("the scan misses the known bad line")
    if unbound_fstring_names(good):
        failures.append("the scan flags the corrected line")
    scanned = 0
    for p in sorted(REPO.rglob("*.py")):
        if SKIP & set(p.relative_to(REPO).parts):
            continue
        scanned += 1
        try:
            hits = unbound_fstring_names(p.read_text(encoding="utf-8"), str(p))
        except SyntaxError as exc:
            failures.append(f"{p.relative_to(REPO)}: does not parse ({exc})")
            continue
        failures += [f"{p.relative_to(REPO)}:{ln}: f-string reads unbound name {name!r}" for ln, name in hits]
    if scanned < 50:
        failures.append(f"only {scanned} modules scanned; the walk is broken")
    for f in failures:
        print("FAIL", f)
    print(f"{'PASS' if not failures else 'FAILED'}: {scanned} modules scanned, {len(failures)} failure(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
