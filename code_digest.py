"""Code package digest for the panel.

A panel can call an analysis "reproducible" without having opened the
code, even when the shipped scripts implement a different measure from the one the
paper defines. The panel cannot judge a claim about code it has not seen, so
this module fetches the author's DECLARED code/data archive, read-only, and
puts in front of the reviewers:

  * the head of its README,
  * the function/definition inventory of every source file,
  * the lines that define or assign the constructs the paper names (its
    acronyms and named measures, e.g. XRM, XRM-2, NPT), with file:line, and
  * the file listing (name, size), grouped by folder and type, last, so it
    is the part a size cap cuts.

What it never does: execute anything, write the archive to disk, follow more
than a size cap of bytes, or touch a link the author did not declare. ICSAC's
"never host authors' code" policy (2026-09-28) is about hosting; this is a
bounded read, in memory, discarded after the digest is written. Sources:
Zenodo records (files listed by the API, zip members read in memory), GitHub
repositories (the zipball), and nothing else -- an unknown host is named in
the digest as "not fetched" so the panel knows the claim stands unchecked.

The digest is untrusted author content: it goes INSIDE the submission block
of the panel prompt, under the same defensive preamble as the manuscript,
and through the blind-review span removal like the abstract. Fail OPEN: any
error yields a one-line digest that says what could not be fetched.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import config  # noqa: E402

USER_AGENT = "icsac-editorial-system/1.0 (help@icsacinstitute.org)"
TIMEOUT = 45
MAX_ARCHIVE_BYTES = int(getattr(config, "CODE_DIGEST_MAX_BYTES", 25 * 1024 * 1024))
MAX_MEMBER_BYTES = 400 * 1024        # a source file bigger than this is listed, not read
MAX_SOURCE_FILES_READ = 60
MAX_FILES_FETCHED = 60               # Zenodo files fetched one by one; the rest are listed only
FETCH_WALL_SEC = 180                 # and never for longer than this in total
MAX_DIGEST_CHARS = int(getattr(config, "CODE_DIGEST_MAX_CHARS", 14000))
README_LINES = 40
HITS_PER_TERM = 12
MAX_TERMS = 12
DEFS_PER_FILE = 15

SOURCE_EXT = {".py", ".r", ".R", ".jl", ".m", ".js", ".ts", ".java", ".c", ".cpp", ".h",
              ".rs", ".go", ".scala", ".sh", ".ipynb", ".sql", ".do", ".sas", ".nb", ".f90"}
TEXT_EXT = SOURCE_EXT | {".md", ".txt", ".rst", ".cfg", ".toml", ".yaml", ".yml", ".json", ".csv", ".tsv"}
_ACRONYM_RE = re.compile(r"\b(?:Δ|Δ-)?[A-Z][A-Za-z]?-?[A-Z]{2,}(?:-[A-Z]{2,})?\b")
_ACRONYM_STOP = {"DOI", "PDF", "URL", "HTTP", "HTTPS", "ORCID", "ISSN", "ISBN", "USA", "UK", "EU",
                 "AI", "LLM", "GPT", "API", "CSV", "JSON", "MIT", "GNU", "GPL", "CC", "BY", "SA",
                 "ND", "NC", "IEEE", "ACM", "PNAS", "PLOS", "MDPI", "ETC", "ET", "AL", "II", "III",
                 "IV", "VI", "VII", "AND", "OR", "THE", "FOR", "NOT", "ARXIV", "ZENODO", "GITHUB",
                 "README", "LICENSE", "COVID", "USD", "EUR", "UTC", "GMT", "ISO", "ANSI", "ASCII",
                 "UTF", "IBM", "NASA", "NIH", "NSF", "ERC", "PI", "RQ", "FAQ", "TL", "DR", "OK",
                 "NB", "PS", "CC-BY", "CC-BY-SA", "SD", "SE", "CI"}
_DEF_RE = re.compile(
    r"^\s*(?:def\s+(\w+)|class\s+(\w+)|function\s+(\w+)|(\w+)\s*(?:<-|=)\s*function\b"
    r"|(?:export\s+)?(?:async\s+)?function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:\(|async|function))",
    re.M)


# ─── link discovery ───────────────────────────────────────────────────

def links_from(submission: dict, full_text: str = "") -> list[str]:
    """Every code/data location the author declared: the form's URL, related
    identifiers of supplement type, and repository links in the paper text."""
    out: list[str] = []
    cd = submission.get("code_data") or {}
    if isinstance(cd, dict) and cd.get("url"):
        out.append(str(cd["url"]).strip())
    for r in submission.get("related_identifiers") or []:
        if not isinstance(r, dict):
            continue
        rel = (r.get("relation") or "").lower()
        if rel in ("issupplementto", "issupplementedby", "references", "isreferencedby",
                   "isderivedfrom", "issourceof", "requires", "isrequiredby"):
            ident = (r.get("identifier") or "").strip()
            if ident:
                out.append(ident)
    if full_text:
        try:
            import code_availability
            out.extend(code_availability.find_links(full_text))
        except Exception:
            pass
    seen, uniq = set(), []
    for u in out:
        key = _canonical(u)
        if key and key not in seen:
            seen.add(key)
            uniq.append(u)
    return uniq


def _canonical(u: str) -> str:
    u = u.strip().rstrip("./")
    u = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", u, flags=re.I)
    return u.lower()


def classify(link: str) -> tuple[str, str]:
    """('zenodo', record_id) | ('github', 'owner/repo') | ('other', link)."""
    s = _canonical(link)
    m = re.search(r"10\.5281/zenodo\.(\d+)", s)
    if m:
        return "zenodo", m.group(1)
    m = re.search(r"zenodo\.org/(?:records?|record)/(\d+)", s)
    if m:
        return "zenodo", m.group(1)
    m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)", s)
    if m:
        return "github", f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    return "other", link


# ─── fetching (read-only, in memory, capped) ─────────────────────────

class DigestFetchError(RuntimeError):
    pass


def _get(url: str, accept: str = "*/*", cap: int = MAX_ARCHIVE_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            length = resp.headers.get("Content-Length")
            if length and int(length) > cap:
                raise DigestFetchError(f"{url}: {int(length)} bytes exceeds the {cap}-byte cap")
            buf = resp.read(cap + 1)
    except urllib.error.HTTPError as e:
        raise DigestFetchError(f"{url}: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DigestFetchError(f"{url}: {e}")
    if len(buf) > cap:
        raise DigestFetchError(f"{url}: exceeds the {cap}-byte cap")
    return buf


def fetch_zenodo(record_id: str) -> tuple[dict, list[dict]]:
    """(record metadata, [{name, size, data|None}]) -- data is None past the cap."""
    meta = json.loads(_get(f"https://zenodo.org/api/records/{record_id}", "application/json",
                           cap=4 * 1024 * 1024).decode("utf-8", errors="replace"))
    files = []
    budget = MAX_ARCHIVE_BYTES
    fetched = 0
    started = time.monotonic()
    for f in meta.get("files") or []:
        name = f.get("key") or ""
        size = int(f.get("size") or 0)
        url = ((f.get("links") or {}).get("self")) or ""
        row = {"name": name, "size": size, "data": None}
        if url and size <= budget and (_is_archive(name) or _ext(name) in TEXT_EXT):
            # A file-count and wall-time cap as well as the byte budget: a data
            # record with hundreds of small text files meant hundreds of
            # sequential requests before the panel (audit 2026-10-01 N6).
            if fetched >= MAX_FILES_FETCHED or time.monotonic() - started > FETCH_WALL_SEC:
                row["error"] = "not read (file-count or time cap)"
            else:
                fetched += 1
                try:
                    row["data"] = _get(url, cap=size + 1)
                    budget -= size
                except DigestFetchError as e:
                    row["error"] = str(e)
        files.append(row)
    return meta, files


def fetch_github(owner_repo: str) -> tuple[dict, list[dict]]:
    meta = json.loads(_get(f"https://api.github.com/repos/{owner_repo}",
                           "application/vnd.github+json", cap=1024 * 1024).decode("utf-8", errors="replace"))
    branch = meta.get("default_branch") or "main"
    data = _get(f"https://codeload.github.com/{owner_repo}/zip/refs/heads/{branch}")
    return meta, [{"name": f"{owner_repo}@{branch}.zip", "size": len(data), "data": data}]


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower() if not name.endswith(".R") else ".R"


def _is_archive(name: str) -> bool:
    return name.lower().endswith((".zip",))


def expand(files: list[dict]) -> list[dict]:
    """Flatten zip archives into member rows [{path, size, text|None}] without
    writing anything to disk. Members past MAX_MEMBER_BYTES are listed only."""
    rows: list[dict] = []
    for f in files:
        data = f.get("data")
        if data is None:
            rows.append({"path": f["name"], "size": f["size"], "text": None,
                         "note": f.get("error") or "not read (size cap or type)"})
            continue
        if _is_archive(f["name"]):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                rows.append({"path": f["name"], "size": f["size"], "text": None, "note": "not a zip file"})
                continue
            read_budget = MAX_ARCHIVE_BYTES
            for m in zf.infolist():
                if m.is_dir() or "__MACOSX/" in m.filename or "/." in "/" + m.filename:
                    continue
                row = {"path": m.filename, "size": m.file_size, "text": None}
                if _ext(m.filename) in TEXT_EXT and m.file_size <= MAX_MEMBER_BYTES and m.file_size <= read_budget:
                    try:
                        with zf.open(m) as fh:
                            raw = fh.read(MAX_MEMBER_BYTES + 1)
                        read_budget -= len(raw)
                        row["text"] = raw.decode("utf-8", errors="replace")
                    except Exception as e:  # corrupt member, encrypted, bomb guard
                        row["note"] = f"unreadable: {type(e).__name__}"
                rows.append(row)
        else:
            text = data.decode("utf-8", errors="replace") if _ext(f["name"]) in TEXT_EXT else None
            rows.append({"path": f["name"], "size": f["size"], "text": text})
    return rows


# ─── analysis ─────────────────────────────────────────────────────────

def constructs_from_text(full_text: str, extra: list[str] | None = None) -> list[str]:
    """The paper's named constructs: acronyms and hyphenated acronym forms,
    frequency-ranked, minus boilerplate. 'ΔXRM' and 'D-XRM' both fold to
    terms the grep will find."""
    counts: Counter = Counter()
    for m in _ACRONYM_RE.finditer(full_text or ""):
        tok = m.group(0).replace("Δ-", "").replace("Δ", "")
        core = tok.split("-")[-1] if "-" in tok else tok
        if core.upper() in _ACRONYM_STOP or tok.upper() in _ACRONYM_STOP or len(core) < 2:
            continue
        counts[tok] += 1
    terms = [t for t, n in counts.most_common(MAX_TERMS * 2) if n >= 3][:MAX_TERMS]
    for t in extra or []:
        if t and t not in terms:
            terms.insert(0, t)
    return terms[:MAX_TERMS]


def _notebook_code(text: str) -> str:
    try:
        nb = json.loads(text)
        cells = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
        return "\n".join("".join(c.get("source", [])) for c in cells)
    except Exception:
        return ""


def _source_lines(row: dict) -> list[str]:
    text = row.get("text") or ""
    if row["path"].lower().endswith(".ipynb"):
        text = _notebook_code(text)
    return text.splitlines()


def definitions(rows: list[dict]) -> list[tuple[str, list[str]]]:
    out = []
    for r in rows:
        if _ext(r["path"]) not in SOURCE_EXT or not r.get("text"):
            continue
        names = []
        for m in _DEF_RE.finditer("\n".join(_source_lines(r))):
            name = next((g for g in m.groups() if g), None)
            if name and name not in names:
                names.append(name)
        if names:
            out.append((r["path"], names[:DEFS_PER_FILE]))
    return out


def term_hits(rows: list[dict], terms: list[str]) -> dict[str, list[str]]:
    """For each term, the source lines that DEFINE it first: a def/class whose
    name carries the term, then an assignment whose left-hand identifier
    carries it (`xrm = a * b + c`, `xrm_value = compute_xrm(`),
    then any other line naming it, with prints, labels, docstrings and
    comments last; `path:line: text`, capped per term. Identifier matching is
    case-insensitive with `-` folded to `_` so XRM-2 finds xrm_2."""
    hits: dict[str, list[str]] = {}
    for term in terms:
        ident = re.escape(term.replace("-", "_").lower())
        exact = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9])")
        in_ident = re.compile(r"(?<![a-z0-9])" + ident + r"(?![a-z0-9])|_" + ident + r"(?![a-z0-9])|" + ident + r"_", re.I)
        defn = re.compile(r"^\s*(?:def|class|function)\s+\w*" + ident + r"\w*\s*[(:]", re.I)
        assign = re.compile(r"^\s*(?:[\w.\[\]\"']*" + ident + r"[\w.\[\]\"']*)\s*(?:<-|=(?!=))", re.I)
        noise = re.compile(r"^\s*(?:#|//|\"\"\"|''')|\bprint\s*\(|\.set_[xy]?label|\blogging\.|\blog\.(?:info|debug)|^\s*f?[\"']", re.I)
        ranked: list[tuple[int, str]] = []
        for r in rows:
            if _ext(r["path"]) not in SOURCE_EXT or not r.get("text"):
                continue
            for i, line in enumerate(_source_lines(r), 1):
                code = line.split("#")[0]
                if not (exact.search(line) or in_ident.search(code.replace("-", "_"))):
                    continue
                entry = f"{r['path']}:{i}: {line.strip()[:160]}"
                if defn.search(code.replace("-", "_")):
                    rank = 0
                elif assign.search(code.replace("-", "_")):
                    rank = 1
                elif noise.search(line):
                    rank = 3
                else:
                    rank = 2
                ranked.append((rank, entry))
        ranked.sort(key=lambda t: t[0])
        chosen = [e for _, e in ranked[:HITS_PER_TERM]]
        if chosen:
            hits[term] = chosen
    return hits


# Section budgets inside MAX_DIGEST_CHARS. The code comes first and the file
# listing last, so a long listing can never push the definitions and construct
# hits past the cap (2026-11: a 223-file archive's listing filled the whole
# digest and the panel saw no code at all).
README_BUDGET = 2500
DEFS_BUDGET = 4000
HITS_BUDGET = 5500
LISTING_BUDGET = 3000
COLLAPSE_AT = 4          # a folder holding this many data files of one type is one line
COLLAPSE_SOURCE_AT = 20  # source files stay listed by name unless a folder holds this many


def _fit(lines: list[str], budget: int, what: str) -> list[str]:
    """Keep whole lines up to `budget` chars, the closing marker included."""
    if sum(len(l) + 1 for l in lines) <= budget:
        return list(lines)
    out, used = [], 0
    for i, line in enumerate(lines):
        marker = f"  [... {len(lines) - i} more {what} lines not shown (size cap)]"
        if used + len(line) + 1 + len(marker) + 1 > budget:
            out.append(marker)
            break
        out.append(line)
        used += len(line) + 1
    return out


def listing_lines(rows: list[dict], collapse: bool = True) -> list[str]:
    """The file listing; with `collapse`, grouped by folder and file type: a
    folder with COLLAPSE_AT or more data files of one type (COLLAPSE_SOURCE_AT
    for source files) becomes one line with the count and total size, so a
    data folder of hundreds of files costs one line."""
    if not collapse:
        return [f"  {r['path']}  ({r['size']} bytes)" + (f"  [{r['note']}]" if r.get("note") else "")
                for r in rows]
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for r in rows[:5000]:
        key = (os.path.dirname(r["path"]), _ext(r["path"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = []
    for key in order:
        members = groups[key]
        folder, ext = key
        if len(members) >= (COLLAPSE_SOURCE_AT if ext in {e.lower() for e in SOURCE_EXT} else COLLAPSE_AT):
            unread = sum(1 for r in members if r.get("note"))
            size = sum(int(r.get("size") or 0) for r in members)
            tail = f", {unread} not read" if unread else ""
            out.append(f"  {folder + '/' if folder else ''}*{ext or ''}  ({len(members)} files, {size} bytes{tail})")
        else:
            for r in members:
                note = f"  [{r['note']}]" if r.get("note") else ""
                out.append(f"  {r['path']}  ({r['size']} bytes){note}")
    if len(rows) > 5000:
        out.append(f"  ... {len(rows) - 5000} more files not grouped")
    return out


def render(source_label: str, meta_line: str, rows: list[dict], terms: list[str]) -> str:
    cap = MAX_DIGEST_CHARS
    # The section budgets shrink with a smaller configured cap, leaving room
    # for the head and the listing.
    scale = min(1.0, max(0.1, (cap - 1500) / (README_BUDGET + DEFS_BUDGET + HITS_BUDGET)))
    head = [f"Source: {source_label[:200]}", meta_line[:260]]
    sections: list[list[str]] = []
    readme = next((r for r in rows if os.path.basename(r["path"]).lower().startswith("readme") and r.get("text")), None)
    if readme:
        body = ["  " + l[:200] for l in readme["text"].splitlines()[:README_LINES]]
        sections.append([f"README head ({readme['path'][:160]}, first {README_LINES} lines):"]
                        + _fit(body, int(README_BUDGET * scale), "README"))
    defs = definitions(rows)
    if defs:
        body = [f"  {path[:200]}: {', '.join(names)[:400]}" for path, names in defs[:MAX_SOURCE_FILES_READ]]
        if len(defs) > MAX_SOURCE_FILES_READ:
            body.append(f"  ... {len(defs) - MAX_SOURCE_FILES_READ} more source files with definitions not shown")
        sections.append(["Definitions per source file:"] + _fit(body, int(DEFS_BUDGET * scale), "definition"))
    hits = term_hits(rows, terms)
    body = []
    for term in terms:
        if term in hits:
            body.append(f"  {term}:")
            body += ["    " + h for h in hits[term]]
        else:
            body.append(f"  {term}: not found in any source file")
    sections.append([f"Paper constructs searched in the code: {', '.join(terms)[:300] or '(none found in the text)'}"]
                    + _fit(body, int(HITS_BUDGET * scale), "construct-hit"))
    text = "\n".join(head)
    for sec in sections:
        text += "\n\n" + "\n".join(sec)
    # The listing goes last and takes what is left, never more than its own
    # budget: every file by name when that fits, else grouped by folder.
    title = f"Files ({len(rows)}, grouped by folder and type):"
    room = min(LISTING_BUDGET, cap - len(text) - len(title) - 4)
    plain = listing_lines(rows, collapse=False)
    if sum(len(l) + 1 for l in plain) <= room:
        title, listing = f"Files ({len(rows)}):", plain
    else:
        listing = listing_lines(rows)
    text += "\n\n" + "\n".join([title] + _fit(listing, max(0, room), "listing"))
    if len(text) > cap:
        # Only reachable with a very small configured cap: cut at a line end.
        cut = text.rfind("\n", 0, cap - 40)
        text = text[:max(cut, 0)] + "\n[digest cut at the size cap]"
    return text


# ─── entry point ──────────────────────────────────────────────────────

def build(submission: dict, full_text: str, *, log=print, extra_terms: list[str] | None = None) -> tuple[str, dict]:
    """Returns (digest_markdown, meta). Never raises. meta records what was
    fetched, how many bytes, and every link that was not."""
    meta = {"enabled": bool(getattr(config, "CODE_DIGEST_ENABLED", True)), "links": [],
            "fetched": None, "bytes": 0, "files": 0, "skipped": [], "error": None}
    if not meta["enabled"]:
        return "(code package digest disabled)", meta
    links = links_from(submission, full_text)
    meta["links"] = links
    if not links:
        return "(none: the submission declares no code or data location)", meta
    terms = constructs_from_text(full_text, extra_terms)
    for link in links:
        kind, ref = classify(link)
        try:
            if kind == "zenodo":
                rec, files = fetch_zenodo(ref)
                title = ((rec.get("metadata") or {}).get("title")) or ""
                label = f"Zenodo record {ref} (declared by the author)"
                meta_line = f"Record title: {title[:200]}"
            elif kind == "github":
                rec, files = fetch_github(ref)
                label = f"GitHub repository {ref} (declared by the author)"
                meta_line = f"Default branch: {rec.get('default_branch')}; description: {(rec.get('description') or '')[:200]}"
            else:
                meta["skipped"].append({"link": link, "why": "host not fetched (only Zenodo and GitHub are read)"})
                continue
        except DigestFetchError as e:
            meta["skipped"].append({"link": link, "why": str(e)[:200]})
            log(f"  code digest: {link}: {e}")
            continue
        except Exception as e:
            meta["skipped"].append({"link": link, "why": f"{type(e).__name__}: {e}"[:200]})
            log(f"  code digest: {link}: {type(e).__name__}: {e}")
            continue
        rows = expand(files)
        meta.update({"fetched": link, "bytes": sum(f["size"] for f in files if f.get("data") is not None),
                     "files": len(rows), "terms": terms})
        digest = render(label, meta_line, rows, terms)
        if meta["skipped"]:
            digest += "\n\nNot fetched: " + "; ".join(f"{s['link']} ({s['why']})" for s in meta["skipped"])
        others = [l for l in links if l != link]
        if others:
            digest += "\n\nOther declared locations (not read; one package per digest): " + ", ".join(others)
        log(f"  code digest: {link}: {len(rows)} files, {meta['bytes']} bytes read, {len(terms)} constructs")
        return digest, meta
    why = "; ".join(f"{s['link']} ({s['why']})" for s in meta["skipped"]) or "no fetchable link"
    meta["error"] = why
    return f"(declared but not fetched: {why})", meta


if __name__ == "__main__":
    # `code_digest.py <sub_dir>` -- print the digest for a submission on disk.
    if len(sys.argv) != 2:
        print("usage: code_digest.py <submission dir>"); sys.exit(2)
    d = sys.argv[1]
    sub = json.load(open(os.path.join(d, "submission.json")))
    import submission_intake
    text = submission_intake.extract_pdf_text(os.path.join(d, "paper.pdf"))
    md, m = build(sub, text)
    print(md)
    print("\n--- meta ---\n" + json.dumps({k: v for k, v in m.items() if k != "terms"}, indent=2))
