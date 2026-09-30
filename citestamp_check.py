"""Citation graph check against CiteStamp (citestamp.com) -- Phase 3 of citation
verification, after resolution (Phase 1) and misattribution (Phase 2).

CiteStamp is ICSAC's sister product: a typed, identity-resolved citation graph
that separates asserted (human-signed) edges from inferred (machine-derived)
ones and carries a replication tier (FORRT FReD supports/refutes). For every
cited work the resolver pinned to a DOI, this module asks the graph:

  in_graph      does CiteStamp know the work at all (any edge about it)?
  edges         how many edges are on record (cites + cited-by, all tiers)
  refuted_by    works recorded as REFUTING it (failed replications, signed
                refutations) -- the one that matters for citation integrity
  supported_by  works recorded as SUPPORTING it

It talks to the public MCP endpoint over plain HTTP (JSON-RPC 2.0, stateless,
no token required; an optional bearer token is honoured). Fail OPEN: if
CiteStamp is unreachable the report says so and the panel proceeds -- this is
an extra lens on the citations, not a gate. Retraction status is NOT exposed by
the MCP tools yet and is reported as "not checked", never as "none".

The attribution footer is built from what the graph actually returned (tiers,
sources, snapshot release) so the report never claims coverage it did not use.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import config  # noqa: E402

MCP_URL = getattr(config, "CITESTAMP_MCP_URL", "https://mcp.citestamp.com/mcp")
USER_AGENT = "icsac-editorial-system/1.0 (help@icsacinstitute.org)"
TIMEOUT = 30
MAX_429_WAIT = 90      # seconds; one retry, then the DOI is reported UNCHECKED
CALL_GAP = 0.25        # be a polite anonymous client
_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)


class CiteStampUnavailable(RuntimeError):
    pass


def _rpc(method: str, params: dict, rpc_id: int, *, _retry: bool = True) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(MCP_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    # Both are required: the edge 403s urllib's default UA, and the server
    # negotiates on Accept (streamable HTTP).
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("User-Agent", USER_AGENT)
    token = getattr(config, "CITESTAMP_MCP_TOKEN", "") or os.environ.get("CITESTAMP_MCP_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        body_txt = e.read()[:300].decode(errors="replace")
        if e.code == 429 and _retry:
            # Anonymous ceiling (seen 2026-09-27 after ~48 calls). The body names
            # the wait; sleep it once (capped) and try again, then give up.
            wait = MAX_429_WAIT
            try:
                wait = min(int(json.loads(body_txt).get("retry_after_s", wait)), MAX_429_WAIT)
            except Exception:
                pass
            time.sleep(max(1, wait))
            return _rpc(method, params, rpc_id, _retry=False)
        raise CiteStampUnavailable(f"HTTP {e.code}: {body_txt[:200]}")
    except Exception as e:  # DNS, timeout, TLS
        raise CiteStampUnavailable(str(e))
    # Streamable HTTP may answer as SSE ("data: {...}") or plain JSON.
    if raw.lstrip().startswith("data:"):
        raw = "\n".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
    try:
        msg = json.loads(raw)
    except ValueError:
        raise CiteStampUnavailable(f"non-JSON reply: {raw[:120]!r}")
    if "error" in msg:
        raise CiteStampUnavailable(f"rpc error: {str(msg['error'])[:200]}")
    return msg.get("result") or {}


def server_info() -> dict:
    res = _rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                              "clientInfo": {"name": "icsac-editorial-system", "version": "1.0"}}, 1)
    return res.get("serverInfo") or {}


def tool(name: str, identifier: str, limit: int = 25, rpc_id: int = 2) -> dict:
    """tools/call -> structuredContent ({groundings, page}). Empty dict on isError."""
    res = _rpc("tools/call", {"name": name, "arguments": {"identifier": identifier, "limit": limit}}, rpc_id)
    if res.get("isError"):
        raise CiteStampUnavailable(f"{name}({identifier}) isError: {str(res.get('content'))[:200]}")
    sc = res.get("structuredContent")
    if not isinstance(sc, dict) or "page" not in sc:
        # A 200 with no structured payload is not an answer. Treating it as
        # "no edges" would report a live work as absent from the graph.
        raise CiteStampUnavailable(f"{name}({identifier}) returned no structuredContent")
    return sc


def _doi_of(c: dict) -> Optional[str]:
    for key in ("doi", "resolved_id"):
        v = (c.get(key) or "").strip()
        if v.lower().startswith("https://doi.org/"):
            v = v[16:]
        if _DOI_RE.fullmatch(v):
            return v
    return None


def check_citations(citations: list[dict], *, log=print) -> dict:
    """Run the three questions for every DOI-pinned citation.

    Returns {"available": bool, "server": {...}, "checked": n, "results": [...],
             "sources": {attribution: n}, "openalex_release": str|None,
             "refuted": n, "supported": n, "in_graph": n, "error": str|None}
    """
    out = {"available": False, "server": {}, "queried": 0, "checked": 0, "unchecked": 0,
           "results": [], "sources": {}, "tiers": {}, "openalex_release": None,
           "refuted": 0, "supported": 0, "in_graph": 0, "error": None}
    try:
        out["server"] = server_info()
    except CiteStampUnavailable as e:
        out["error"] = f"CiteStamp unreachable: {e}"
        log(f"  citestamp: {out['error']}")
        return out
    out["available"] = True
    rpc_id = 10
    for c in citations:
        doi = _doi_of(c)
        if not doi:
            continue
        row = {"doi": doi, "raw": (c.get("raw") or "")[:160], "in_graph": False, "edges": 0,
               "refuted_by": [], "supported_by": [], "error": None}
        def _tally(groundings):
            for g in groundings:
                att = g.get("attribution") or "unknown"
                out["sources"][att] = out["sources"].get(att, 0) + 1
                tier = g.get("tier") or "unknown"
                out["tiers"][tier] = out["tiers"].get(tier, 0) + 1
                rel = (g.get("provenance") or {}).get("openalex_release")
                if rel and (out["openalex_release"] is None or rel > out["openalex_release"]):
                    out["openalex_release"] = rel
        try:
            time.sleep(CALL_GAP)
            about = tool("edges_about", doi, limit=1, rpc_id=rpc_id); rpc_id += 1
            page = about.get("page") or {}
            gr = about.get("groundings") or []
            row["edges"] = int(page.get("inferred_total") or 0) + sum(1 for g in gr if g.get("tier") == "asserted")
            row["in_graph"] = bool(gr) or row["edges"] > 0
            _tally(gr)
            if row["in_graph"]:
                time.sleep(CALL_GAP)
                ref = tool("what_refutes", doi, limit=25, rpc_id=rpc_id); rpc_id += 1
                row["refuted_by"] = [{"by": g.get("subject"), "tier": g.get("tier"),
                                      "attribution": g.get("attribution")} for g in ref.get("groundings") or []]
                time.sleep(CALL_GAP)
                sup = tool("what_supports", doi, limit=25, rpc_id=rpc_id); rpc_id += 1
                row["supported_by"] = [{"by": g.get("subject"), "tier": g.get("tier"),
                                        "attribution": g.get("attribution")} for g in sup.get("groundings") or []]
                _tally((ref.get("groundings") or []) + (sup.get("groundings") or []))
        except CiteStampUnavailable as e:
            row["error"] = str(e)[:200]
            log(f"  citestamp: {doi}: {row['error']}")
        out["results"].append(row)
        out["queried"] += 1
        if row["error"]:
            out["unchecked"] += 1
            continue
        out["checked"] += 1
        out["in_graph"] += int(row["in_graph"])
        out["refuted"] += int(bool(row["refuted_by"]))
        out["supported"] += int(bool(row["supported_by"]))
    log(f"  citestamp: {out['queried']} DOIs queried, {out['checked']} answered, {out['unchecked']} unchecked -- "
        f"{out['in_graph']} in graph, {out['refuted']} with refutations on record, {out['supported']} with supports")
    out["coverage_gaps"] = _log_coverage_gaps(citations, out["results"], log=log)
    return out


COVERAGE_GAP_LOG = getattr(config, "CITESTAMP_COVERAGE_GAP_LOG",
                           os.path.join(getattr(config, "REVIEWS_DIR", "reviews"), "citestamp_coverage_gaps.jsonl"))


def _log_coverage_gaps(citations: list[dict], results: list[dict], *, log=print) -> int:
    """A DOI whose identity Crossref/DataCite CONFIRMED and that CiteStamp does
    not know is a coverage gap in the graph, not a doubt about the citation.
    Append each one (DOI, registry title, year) to a JSONL file so the gaps
    can be fed to CiteStamp later. A dead or mismatching DOI is never logged:
    the graph is right not to have it. Returns the number logged."""
    by_doi = {r["doi"].lower(): r for r in results if r.get("doi")}
    rows = []
    for c in citations:
        if c.get("doi_identity") != "confirmed":
            continue
        doi = _doi_of(c)
        r = by_doi.get((doi or "").lower())
        if not doi or not r or r.get("error") or r.get("in_graph"):
            continue
        rows.append({"doi": doi.lower(), "title": (c.get("title") or "")[:300],
                     "year": c.get("year"), "resolver": c.get("resolver"),
                     "logged": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if not rows:
        return 0
    try:
        os.makedirs(os.path.dirname(COVERAGE_GAP_LOG), exist_ok=True)
        with open(COVERAGE_GAP_LOG, "a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        log(f"  citestamp: {len(rows)} confirmed DOI(s) not in the graph logged to {COVERAGE_GAP_LOG}")
    except OSError as e:
        log(f"  citestamp: coverage-gap log not written ({e})")
    return len(rows)


def attribution_line(res: dict) -> str:
    """One sentence, built only from what came back."""
    if not res.get("available"):
        return "Citation graph check: CiteStamp was unavailable for this submission."
    ver = (res.get("server") or {}).get("version") or "?"
    srcs = sorted(res.get("sources") or {})
    src_txt = ", ".join(srcs) if srcs else "no edges returned"
    tiers = sorted(res.get("tiers") or {})
    tier_txt = ("tiers seen: " + " + ".join(tiers)) if tiers else "no edge tiers returned"
    rel = f"; OpenAlex release {res['openalex_release']}" if res.get("openalex_release") else ""
    unchecked = (f" {res.get('unchecked', 0)} DOI(s) could not be checked (rate limit/transport) and are "
                 f"reported as unchecked, not as absent." if res.get("unchecked") else "")
    return (f"Citations verified by CiteStamp (citestamp.com, graph server v{ver}; {tier_txt}; "
            f"sources seen: {src_txt}{rel}): {res['in_graph']}/{res['checked']} checked DOIs are in the graph, "
            f"{res['refuted']} with a refutation on record, {res['supported']} with a replication/support on record."
            f"{unchecked} Retraction status is not exposed by the graph and was not checked.")


def summary_line(res: dict) -> str:
    """Short form for the review header."""
    if not res.get("available"):
        return "CiteStamp: unavailable"
    return (f"CiteStamp: {res['in_graph']}/{res['checked']} DOIs in graph, "
            f"{res['refuted']} refuted, {res['supported']} supported"
            + (f", {res['unchecked']} unchecked" if res.get("unchecked") else ""))


def merge_into_verification_report(report: str, res: dict) -> str:
    """Append the Phase 3 section to the citation verification report."""
    lines = ["", "## Citation graph check (CiteStamp)", ""]
    if not res.get("available"):
        lines += [res.get("error") or "CiteStamp unavailable.", "", "---", ""]
        return report.rstrip() + "\n" + "\n".join(lines)
    lines += [f"{res.get('queried', res['checked'])} DOI-resolved citations queried, {res['checked']} answered"
              + (f", {res['unchecked']} unchecked" if res.get("unchecked") else "") + ". "
              f"{res['in_graph']} are in the graph; {res['refuted']} have a refutation on record; "
              f"{res['supported']} have a recorded support/replication.", ""]
    flagged = [r for r in res["results"] if r["refuted_by"]]
    if flagged:
        lines += ["**Refutations on record -- the panel should read these before scoring citation integrity:**", ""]
        for r in flagged:
            who = "; ".join(f"{x['by']} ({x['tier']}, {x['attribution']})" for x in r["refuted_by"][:5])
            lines.append(f"- `{r['doi']}` -- refuted by {who}")
        lines.append("")
    lines += ["| DOI | In graph | Edges | Refuted by | Supported by |", "|---|---|---|---|---|"]
    for r in res["results"]:
        if r["error"]:
            lines.append(f"| `{r['doi']}` | unchecked | – | – | – |")
        else:
            lines.append(f"| `{r['doi']}` | {'yes' if r['in_graph'] else 'no'} | {r['edges']} | "
                         f"{len(r['refuted_by'])} | {len(r['supported_by'])} |")
    lines += ["", f"_{attribution_line(res)}_", "", "---", ""]
    return report.rstrip() + "\n" + "\n".join(lines)


if __name__ == "__main__":
    # `citestamp_check.py <record_id>` -- re-run Phase 3 on a saved citations file.
    if len(sys.argv) != 2:
        print("usage: citestamp_check.py <record_id>"); sys.exit(2)
    p = os.path.join(config.REVIEWS_DIR, f"{sys.argv[1]}_citations.json")
    payload = json.load(open(p))
    res = check_citations(payload.get("citations") or [])
    print(attribution_line(res))
    for r in res["results"]:
        print(f"  {r['doi']:45s} in_graph={r['in_graph']} edges={r['edges']} refuted={len(r['refuted_by'])} supported={len(r['supported_by'])}")
