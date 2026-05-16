"""ICSAC paper submission intake — FastAPI handler.

Origin-trust is enforced by HMAC over body+timestamp using a shared
secret known only to the upstream proxy (CF Pages Function) and this
handler; the handler is bound to a private interface and only reachable
through that proxy.

Endpoints:
  POST /api/submit                    — multipart form + PDF, HMAC-gated
  GET  /api/submission/<id>/state     — public read-only status

The handler writes ~/icsac-submissions/<id>/{paper.pdf, submission.json,
state.json}, drops a marker in queue/, fires the confirmation email and
a curator alert via the configured notification channel, and returns
immediately. The pipeline runs in submission_worker.py triggered by a
systemd .path unit on the queue directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# Parent-package imports (editorial-system modules live at repo root, one
# level above this intake/ subpackage).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402

import submission_intake as ingest  # noqa: E402  — pipeline module
import notify  # noqa: E402

from . import notify_author  # local
from .time_fmt import to_et_display  # local


SUBMISSIONS_ROOT = Path.home() / "icsac-submissions"
QUEUE_DIR = SUBMISSIONS_ROOT / "queue"
COUNTER_FILE = SUBMISSIONS_ROOT / ".counter"
AUDIT_LOG = Path(config.REVIEWS_DIR) / "audit-log.jsonl"
# Test-mode isolation: test submissions live in a separate state subtree and
# write to a sibling audit log so production observability never sees them.
# A test entry never leaks into AUDIT_LOG; production observability never
# sees TEST_AUDIT_LOG. Belt-and-suspenders with the `test: true` field.
TEST_SUBMISSIONS_ROOT = SUBMISSIONS_ROOT / "test"
TEST_QUEUE_DIR = TEST_SUBMISSIONS_ROOT / "queue"
TEST_AUDIT_LOG = Path(config.REVIEWS_DIR) / "audit-log-test.jsonl"

MAX_PDF_BYTES = int(os.environ.get(
    "INTAKE_MAX_PDF_BYTES", str(100 * 1024 * 1024)
))  # default 100 MB — matches the Cloudflare Pages free-tier request cap
HMAC_SECRET = os.environ.get("INTAKE_HMAC_SECRET", "").encode()
HMAC_MAX_SKEW_SEC = 300

ALLOWED_LICENSES = {"cc-by-4.0", "cc-by-sa-4.0", "cc0-1.0"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
SUB_ID_RE = re.compile(r"^ICSAC-SUB-\d{5}$")
# Test-mode IDs are visually distinct: ICSAC-SUB-TEST-<unix-ts>. Both the
# real and test ID regexes are accepted by the public state endpoint so
# the website's polling round-trip exercises in test mode without code
# changes downstream of the handler.
TEST_SUB_ID_RE = re.compile(r"^ICSAC-SUB-TEST-\d{10,}$")

# ── ORCID test-mode whitelist ───────────────────────────────────
# Test mode is OFF unless TEST_ORCIDS env var lists one or more ORCID iDs.
# Empty default keeps a public forker's deployment in production-only mode.
# Submissions authenticated with an ORCID in this set short-circuit the
# real review pipeline: they exercise the submit/state-page round trip
# but do NOT run the AI panel, RQC, Zenodo deposit, author email, or
# Telegram curator-approval. Audit entries are written with `test: true`
# so consumers can filter.
_DEFAULT_TEST_ORCIDS = frozenset()


def _normalize_orcid(s: str) -> str:
    """Whitespace-strip and uppercase the X check digit. ORCID ids are
    case-sensitive on the final character only."""
    return (s or "").strip().upper()


def _load_test_orcids() -> frozenset[str]:
    raw = os.environ.get("TEST_ORCIDS", "").strip()
    if not raw:
        return _DEFAULT_TEST_ORCIDS
    parsed = {_normalize_orcid(p) for p in raw.split(",") if p.strip()}
    # Union with fallback: env var can extend, never accidentally empty
    # the whitelist below the hardcoded floor.
    return frozenset(parsed | _DEFAULT_TEST_ORCIDS)


TEST_ORCID_WHITELIST = _load_test_orcids()


def is_test_submission(orcid: str) -> bool:
    """Return True if `orcid` is on the ICSAC test-mode whitelist."""
    return _normalize_orcid(orcid) in TEST_ORCID_WHITELIST


# ── tier-encoding test tokens ──────────────────────────────────
# Test-mode replaces the real ORCID at every persistence/transmission
# boundary with a 9-char tier-encoding token of the form
# `LL[slot]LL[slot]LL[tier]`. Letter sets are disjoint per tier so a
# token can be cheaply tier-identified by inspection. Trailing char is
# always the tier digit; two interior slots pick 0 or the tier digit.
# 2880 variants per tier. Pass `seed=sub_id` for a stable token across
# all log lines for one submission; `seed=None` for fresh randomness
# per call (used at transmission-boundary call sites where stability
# isn't needed). The real ORCID stays in-memory for OAuth verification
# but never reaches disk, journald, Telegram, or the test state file.
_TIER_LETTERS = {1: "azbycx", 2: "dwevfu", 3: "gthsir"}


def _test_token(tier: int, *, seed: str | None = None) -> str:
    rng = secrets.SystemRandom() if seed is None else random.Random(seed)
    letters = list(_TIER_LETTERS[tier])
    rng.shuffle(letters)
    d = str(tier)
    s1 = rng.choice(("0", d))
    s2 = rng.choice(("0", d))
    return f"TEST:{letters[0]}{letters[1]}{s1}{letters[2]}{letters[3]}{s2}{letters[4]}{letters[5]}{d}"


_TEST_TOKEN_RE = {
    1: re.compile(r"^TEST:[abcxyz]{2}[01][abcxyz]{2}[01][abcxyz]{2}1$"),
    2: re.compile(r"^TEST:[defuvw]{2}[02][defuvw]{2}[02][defuvw]{2}2$"),
    3: re.compile(r"^TEST:[ghirst]{2}[03][ghirst]{2}[03][ghirst]{2}3$"),
}


def _identify_test_token(token: str) -> int | None:
    for tier, pattern in _TEST_TOKEN_RE.items():
        if pattern.match(token):
            return tier
    return None


# ── tier resolution ────────────────────────────────────────────
# T1 (existing) short-circuits the pipeline with canned responses.
# T2 runs the real panel + RQC, but routes the author email to
# ~/icsac-submissions/test/_outbox/<sub_id>.eml (no SMTP, no IMAP)
# and skips Zenodo + curator Telegram. T3 runs everything real but
# uses sandbox.zenodo.org, drafts to [Gmail]/Drafts with a
# `[T3 TEST] ` subject prefix, and sends curator Telegram to a
# separate test chat. TEST_TIERS_DISABLED env var forces all test
# submissions back to T1 — kill switch in case a tier elevation is
# burning tokens or hitting external systems unexpectedly.

def _tiers_disabled() -> bool:
    return os.environ.get("TEST_TIERS_DISABLED", "").lower() in (
        "true", "1", "yes",
    )


def _resolve_tier(orcid: str, tier_header: str | None) -> int:
    """Resolve effective tier. Returns 1, 2, or 3.

    Production ORCIDs always get 1 even if the header is set (the param
    is ignored — production flow stays unchanged). For test ORCIDs, the
    header drives the choice; garbage values silently downgrade to 1.
    TEST_TIERS_DISABLED env var forces 1 globally for test ORCIDs.
    """
    if not is_test_submission(orcid):
        return 1
    if _tiers_disabled():
        return 1
    if tier_header in ("t2", "T2"):
        return 2
    if tier_header in ("t3", "T3"):
        return 3
    return 1
# DOI mode supports Zenodo + arXiv. Crossref + DataCite generic support is
# the next gap. See project_icsac_submission_intake.md for the resolver
# routing logic. Worker dispatches on these regexes via ingest.is_arxiv_ref.
ZENODO_DOI_RE = re.compile(r"^10\.5281/zenodo\.\d+$", re.IGNORECASE)
ARXIV_REF_RE = re.compile(
    r"^(?:10\.48550/arXiv\.)?\d{4}\.\d{4,5}(?:v\d+)?$", re.IGNORECASE
)
DOI_SHAPE_RE = re.compile(r"^10\.\d{4,9}/\S+$")

app = FastAPI(title="ICSAC Submission Intake", docs_url=None, redoc_url=None)


# ── helpers ─────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_max_known_sub_n() -> int:
    """Walk on-disk submission dirs + the audit-log for ICSAC-SUB-NNNNN
    references. Returns the largest N ever seen, or 0. Belt-and-suspenders
    against a counter-file that's been deleted or stomped during cleanup
    — by deriving the floor from primary sources of truth, allocation
    can never regress to a previously-issued ID even if .counter is reset.
    """
    max_n = 0
    if SUBMISSIONS_ROOT.is_dir():
        for entry in SUBMISSIONS_ROOT.iterdir():
            m = re.match(r"^ICSAC-SUB-(\d+)$", entry.name)
            if m:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
    if AUDIT_LOG.exists():
        try:
            with AUDIT_LOG.open() as alog:
                for line in alog:
                    am = re.search(r'"sub_id":\s*"ICSAC-SUB-(\d+)"', line)
                    if am:
                        n = int(am.group(1))
                        if n > max_n:
                            max_n = n
        except Exception:
            pass
    return max_n


def _allocate_sub_id() -> str:
    """Atomic monotonic counter. The counter file is one of three sources
    of truth (file value, on-disk dirs, audit-log entries); the next ID
    is always max-over-sources + 1, so allocation can't regress even if
    the counter file gets deleted or de-synced during curator cleanup.
    """
    import fcntl

    SUBMISSIONS_ROOT.mkdir(parents=True, exist_ok=True)
    COUNTER_FILE.touch(exist_ok=True)

    with COUNTER_FILE.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                file_value = int((f.read().strip() or "0"))
            except ValueError:
                file_value = 0
            current = max(file_value, _scan_max_known_sub_n())
            nxt = current + 1
            f.seek(0)
            f.truncate()
            f.write(f"{nxt}\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return f"ICSAC-SUB-{nxt:05d}"


def _audit_append(entry: dict, *, test_mode: bool = False) -> None:
    """Append a JSONL audit entry. Test-mode entries route to a separate
    sibling file (TEST_AUDIT_LOG) so production observability never sees
    them — production AUDIT_LOG stays disjoint from test data even if a
    downstream consumer forgets to filter on the `test: true` field."""
    target = TEST_AUDIT_LOG if test_mode else AUDIT_LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    base = {"ts": _now_iso()}
    if test_mode:
        base["test"] = True
    entry = {**base, **entry}
    try:
        with target.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"audit-log append failed: {exc}", file=sys.stderr)


def _tlog(msg: str) -> None:
    """Stdout/journald log with [TEST] prefix so a curator tailing logs
    can instantly see test-mode activity. Flushed so it lands in
    journalctl in real time."""
    print(f"[TEST] {msg}", flush=True)


def _verify_hmac(request: Request, body: bytes) -> None:
    """Reject request unless X-ICSAC-Signature + Timestamp validate."""
    if not HMAC_SECRET:
        raise HTTPException(500, "intake misconfigured: HMAC secret missing")

    sig_header = request.headers.get("x-icsac-signature", "")
    ts_header = request.headers.get("x-icsac-timestamp", "")
    if not sig_header.startswith("sha256=") or not ts_header:
        raise HTTPException(401, "missing signature")

    try:
        ts = int(ts_header)
    except ValueError:
        raise HTTPException(401, "bad timestamp")
    if abs(time.time() - ts) > HMAC_MAX_SKEW_SEC:
        raise HTTPException(401, "stale signature")

    expected = hmac.new(
        HMAC_SECRET, f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig_header[len("sha256="):]):
        raise HTTPException(401, "bad signature")


def _validate_submitter(d: dict) -> dict:
    """Validate the always-required submitter fields. Raises 400 on bad input.

    ORCID is required on both DOI and upload routes since 2026-04-27 (frontend
    commit f5f5305 + backend enforcement here closes the DevTools-bypass gap).

    `exclusivity` is opt-in — frontend has been sending it on PDF submissions
    since website commit 57565a4 (2026-04-26). We capture the value when
    present (alongside coi_acknowledged) but do not enforce it server-side;
    the form-level requirement lives in the upload UI, not the API contract.
    """
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    orcid = (d.get("orcid") or "").strip()
    coi = str(d.get("coi") or "").lower() in ("on", "true", "1", "yes")
    exclusivity_raw = d.get("exclusivity")
    if exclusivity_raw is None:
        exclusivity = None
    else:
        exclusivity = str(exclusivity_raw).lower() in ("on", "true", "1", "yes")

    errs = []
    if len(name) < 2 or len(name) > 200:
        errs.append("name must be 2–200 chars")
    if not EMAIL_RE.match(email) or len(email) > 200:
        errs.append("email is not a valid address")
    if not orcid:
        errs.append("ORCID is required")
    elif not ORCID_RE.match(orcid):
        errs.append("ORCID must be of the form 0000-0000-0000-0000")
    if not coi:
        errs.append("conflict-of-interest acknowledgement required")
    if errs:
        raise HTTPException(400, {"error": "validation_failed", "details": errs})

    return {
        "name": name, "email": email,
        "orcid": orcid, "coi_acknowledged": True,
        "exclusivity_acknowledged": exclusivity,
    }


RESOURCE_TYPES = {"preprint", "article", "report", "dataset", "software", "other"}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELATION_TYPES = {
    "isSupplementTo", "isPreviousVersionOf", "isNewVersionOf",
    "isDerivedFrom", "isPartOf", "cites", "references", "isDocumentedBy",
}


def _parse_creators(raw: str) -> list[dict]:
    """Parse the JSON-encoded `creators` field from the upload form.

    Each entry must have a non-empty name; orcid (validated against ORCID_RE)
    and affiliation are optional. Raises HTTPException 400 on invalid shape
    or invalid ORCID; the caller catches errors via the standard validator
    accumulator. Returns [] if raw is empty so the caller can decide whether
    that's acceptable (it's not — at least one creator is required).
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, {"error": "validation_failed",
                                  "details": ["creators must be JSON"]})
    if not isinstance(parsed, list):
        raise HTTPException(400, {"error": "validation_failed",
                                  "details": ["creators must be a JSON array"]})
    out = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise HTTPException(400, {"error": "validation_failed",
                                      "details": [f"creators[{i}] must be an object"]})
        name = (entry.get("name") or "").strip()
        if not name or len(name) > 200:
            raise HTTPException(400, {"error": "validation_failed",
                                      "details": [f"creators[{i}].name required (≤200 chars)"]})
        orcid = (entry.get("orcid") or "").strip()
        if orcid and not ORCID_RE.match(orcid):
            raise HTTPException(400, {"error": "validation_failed",
                                      "details": [f"creators[{i}].orcid must be 0000-0000-0000-0000 form"]})
        affiliation = (entry.get("affiliation") or "").strip()
        rec: dict[str, str] = {"name": name}
        if orcid:
            rec["orcid"] = orcid
        if affiliation:
            rec["affiliation"] = affiliation[:300]
        out.append(rec)
    return out


def _parse_related_identifiers(raw: str) -> list[dict]:
    """Parse the JSON-encoded `related_identifiers` field — optional, may
    be absent or an empty list. Each entry needs a non-empty `identifier`
    and a `relation` from the Zenodo-aligned RELATION_TYPES set.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, {"error": "validation_failed",
                                  "details": ["related_identifiers must be JSON"]})
    if not isinstance(parsed, list):
        raise HTTPException(400, {"error": "validation_failed",
                                  "details": ["related_identifiers must be a JSON array"]})
    out = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise HTTPException(400, {"error": "validation_failed",
                                      "details": [f"related_identifiers[{i}] must be an object"]})
        ident = (entry.get("identifier") or "").strip()
        rel = (entry.get("relation") or "").strip()
        if not ident or len(ident) > 500:
            raise HTTPException(400, {"error": "validation_failed",
                                      "details": [f"related_identifiers[{i}].identifier required (≤500 chars)"]})
        if rel not in RELATION_TYPES:
            raise HTTPException(400, {"error": "validation_failed",
                                      "details": [f"related_identifiers[{i}].relation must be one of {sorted(RELATION_TYPES)}"]})
        out.append({"identifier": ident, "relation": rel})
    return out


def _validate_upload_metadata(d: dict) -> dict:
    """Validate the metadata fields required only when the submitter is
    uploading a PDF (DOI-mode papers get metadata from the resolver).

    Captures the full Zenodo deposit metadata expansion (2026-04-27):
    creators[], resource_type, publication_date, subject, funding,
    related_identifiers[], and the deposit_consent gate. The deposit
    step (worker-side) consumes these to mint the ICSAC-community Zenodo
    record on accept; until that step ships the metadata sits in
    submission.json waiting for the deposit module.
    """
    title = (d.get("title") or "").strip()
    abstract = (d.get("abstract") or "").strip()
    keywords = (d.get("keywords") or "").strip()
    license_id = (d.get("license") or "").strip().lower()
    resource_type = (d.get("resource_type") or "preprint").strip().lower()
    publication_date = (d.get("publication_date") or "").strip()
    subject = (d.get("subject") or "").strip()
    funding = (d.get("funding") or "").strip()
    deposit_consent = str(d.get("deposit_consent") or "").lower() in ("on", "true", "1", "yes")

    creators = _parse_creators(d.get("creators") or "")
    related = _parse_related_identifiers(d.get("related_identifiers") or "")

    errs = []
    if len(title) < 3 or len(title) > 400:
        errs.append("title must be 3–400 chars")
    if len(abstract) < 50 or len(abstract) > 5000:
        errs.append("abstract must be 50–5000 chars")
    if not keywords:
        errs.append("at least one keyword required")
    if license_id not in ALLOWED_LICENSES:
        errs.append(f"license must be one of {sorted(ALLOWED_LICENSES)}")
    if resource_type not in RESOURCE_TYPES:
        errs.append(f"resource_type must be one of {sorted(RESOURCE_TYPES)}")
    if not publication_date:
        errs.append("publication_date required (YYYY-MM-DD)")
    elif not ISO_DATE_RE.match(publication_date):
        errs.append("publication_date must be YYYY-MM-DD format")
    if not creators:
        errs.append("at least one creator required")
    if not deposit_consent:
        errs.append("deposit_consent required for upload route")
    if errs:
        raise HTTPException(400, {"error": "validation_failed", "details": errs})

    kw_list = [k.strip() for k in re.split(r"[,;]+", keywords) if k.strip()]
    return {
        "title": title, "abstract": abstract,
        "keywords": kw_list, "license": license_id,
        "resource_type": resource_type,
        "publication_date": publication_date,
        "subject": subject or None,
        "funding": funding or None,
        "creators": creators,
        "related_identifiers": related,
        "deposit_consent": True,
    }


def _normalize_doi(raw: str) -> str:
    """Strip URL prefix variants. Accepts 'https://doi.org/10.5281/...',
    'doi:10.5281/...', or bare DOI."""
    s = (raw or "").strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^doi:\s*", "", s, flags=re.IGNORECASE)
    return s.strip()


# ── test-mode handler ───────────────────────────────────────────

def _allocate_test_sub_id() -> str:
    """ICSAC-SUB-TEST-<unix-ts>. Visually distinct prefix keeps test
    submissions from polluting the real ICSAC-SUB-NNNNN counter or
    on-disk numbering."""
    return f"ICSAC-SUB-TEST-{int(time.time())}"


def _test_state_progression(sub_dir: Path, sub_id: str) -> None:
    """Background coroutine: walk the canned test submission through
    SUBMITTED → REVIEWING → REVIEW_COMPLETE → ACCEPTED on a 5s/10s/15s
    cadence so the state-page poll loop sees realistic transitions.
    Runs as a fire-and-forget task scheduled from handle_test_submission.
    All audit entries are stamped test=True.
    """

    def _write(state: str, **extra) -> None:
        state_path = sub_dir / "state.json"
        data = json.loads(state_path.read_text()) if state_path.exists() else {}
        data["state"] = state
        data["test_mode"] = True
        for k, v in extra.items():
            data[k] = v
        state_path.write_text(json.dumps(data, indent=2))

    async def _run() -> None:
        try:
            await asyncio.sleep(5)
            _write("in_review", review_started_at=_now_iso())
            _audit_append(
                {"sub_id": sub_id, "event": "review_started"},
                test_mode=True,
            )

            await asyncio.sleep(5)
            # Canned panel result — score 7/10 across all rubric dims.
            # Note: the public stats.py rubric runs on a 1-5 scale; the
            # test record is gated out of stats by the test-flag filter
            # so the 7/10 marker is just a visible "this is canned" tell
            # for any curator who reads the state file directly.
            canned_panel = {
                "recommendation": "ACCEPT",
                "scores": {
                    "domain_fit": 7,
                    "methodological_transparency": 7,
                    "internal_consistency": 7,
                    "citation_integrity": 7,
                    "novelty_signal": 7,
                    "ai_provenance_signal": 7,
                },
                "comments": "[TEST MODE — canned response]",
                "scale_note": "scores are 7/10 (test scale, NOT production 1-5)",
            }
            _write("review_complete",
                   panel_completed_at=_now_iso(),
                   panel_result=canned_panel)
            _audit_append(
                {"sub_id": sub_id, "event": "review_completed",
                 "recommendation": "ACCEPT",
                 "panel_result": canned_panel},
                test_mode=True,
            )

            await asyncio.sleep(5)
            _write("completed",
                   completed_at=_now_iso(),
                   decision="accept",
                   panel_recommendation="ACCEPT")
            _audit_append(
                {"sub_id": sub_id, "event": "decision_finalized",
                 "verdict": "accept", "by": "test_mode"},
                test_mode=True,
            )
        except Exception as exc:
            print(f"test-mode progression failed for {sub_id}: {exc}",
                  file=sys.stderr)
            _audit_append(
                {"sub_id": sub_id, "event": "test_progression_failed",
                 "error": str(exc)[:200]},
                test_mode=True,
            )

    # Schedule on the running loop; FastAPI's request handler is async
    # so loop.create_task is the natural fit. We don't await — the
    # response returns immediately and the progression runs in the
    # background until the final ACCEPTED state lands.
    try:
        asyncio.get_event_loop().create_task(_run())
    except RuntimeError:
        # No running loop (shouldn't happen inside an async handler) —
        # fall back to threading so the smoke test still progresses.
        import threading
        threading.Thread(target=lambda: asyncio.run(_run()),
                         daemon=True).start()


def handle_test_submission(submitter: dict, auth_orcid: str,
                           auth_name: str, source: str,
                           source_ref: str, title: str,
                           received_at: str) -> JSONResponse:
    """Test-mode short-circuit. Writes a state file under SUBMISSIONS_ROOT
    using a TEST-prefixed ID, audits with test=True, schedules background
    state progression, and returns the same response shape as the real
    handler. Does NOT dispatch the panel, run RQC, deposit to Zenodo,
    email the author, or fire Telegram approval.
    """
    sub_id = _allocate_test_sub_id()
    token = _test_token(1, seed=sub_id)
    # Test submissions live under SUBMISSIONS_ROOT/test/<sub_id> — disjoint
    # from production submission dirs so a sweep job can `rm -rf test/`
    # weekly without ever touching real records.
    TEST_SUBMISSIONS_ROOT.mkdir(parents=True, exist_ok=True)
    sub_dir = TEST_SUBMISSIONS_ROOT / sub_id
    sub_dir.mkdir(parents=True, exist_ok=False)
    _tlog(f"T1 submission received: {sub_id} (auth_token={token})")

    redacted_form = {**submitter, "orcid": token}
    submission_record = {
        "sub_id": sub_id,
        "test_mode": True,
        "tier": 1,
        "received_at": received_at,
        "source": source,
        "source_ref": source_ref,
        "form": redacted_form,
        "auth": {
            "orcid": token,
            "name_on_record": auth_name or None,
            "verified": bool(auth_orcid),
        },
        "title": title,
        "abstract": "[TEST MODE — canned response]",
    }
    (sub_dir / "submission.json").write_text(
        json.dumps(submission_record, indent=2)
    )
    (sub_dir / "state.json").write_text(json.dumps({
        "state": "received",
        "test_mode": True,
        "tier": 1,
        "received_at": received_at,
    }, indent=2))

    _audit_append({
        "sub_id": sub_id,
        "event": "submission_received",
        "tier": 1,
        "source": source,
        "source_ref": source_ref,
        "title": title[:200],
        "auth_orcid": token,
        "auth_verified": bool(auth_orcid),
    }, test_mode=True)

    _test_state_progression(sub_dir, sub_id)

    return JSONResponse({
        "sub_id": sub_id,
        "status_url": f"/submission/{sub_id}",
        "state": "received",
        "received_at": received_at,
        "test_mode": True,
        "tier": 1,
    })


# ── tier 2 / tier 3 pipeline submission ─────────────────────────

async def handle_test_pipeline_submission(
    *, tier: int, request: Request, form, submitter: dict,
    auth_orcid: str, auth_name: str,
) -> JSONResponse:
    """T2/T3 entry point: real pipeline, test side-effect routing.

    Mirrors the production handler's PDF/DOI handling and metadata
    validation, but writes everything into SUBMISSIONS_ROOT/test/<id>,
    drops the queue marker into TEST_QUEUE_DIR, redacts the live ORCID
    via _test_token(tier, seed=sub_id), and stamps `tier` on
    submission.json so the worker knows to apply T2/T3 routing in the
    side-effect stages (Zenodo sandbox, email outbox vs draft, test
    Telegram chat).

    On T3 only, fires an immediate curator-Telegram tripwire so the
    curator knows real frontier-model tokens are about to burn and
    a Zenodo sandbox call is queued. Tripwire goes to the OPERATOR
    chat (TELEGRAM_CHAT_ID), not the test chat.
    """
    if tier not in (2, 3):
        raise ValueError(f"handle_test_pipeline_submission tier must be 2 or 3, got {tier!r}")

    pdf = form.get("pdf")
    has_pdf = pdf is not None and hasattr(pdf, "read") and getattr(pdf, "filename", "")
    raw_doi = form.get("doi", "") or ""
    doi = _normalize_doi(raw_doi if isinstance(raw_doi, str) else "")
    has_doi = bool(doi)

    if has_doi and has_pdf:
        raise HTTPException(400, {
            "error": "doi_xor_pdf",
            "message": "Provide a DOI OR a PDF upload, not both.",
        })
    if not has_doi and not has_pdf:
        raise HTTPException(400, {
            "error": "doi_or_pdf_required",
            "message": "Provide a DOI or upload a PDF.",
        })

    if has_doi:
        _validate_doi_shape(doi)

    sub_id = _allocate_test_sub_id()
    token = _test_token(tier, seed=sub_id)
    TEST_SUBMISSIONS_ROOT.mkdir(parents=True, exist_ok=True)
    sub_dir = TEST_SUBMISSIONS_ROOT / sub_id
    sub_dir.mkdir(parents=True, exist_ok=False)
    pdf_path = sub_dir / "paper.pdf"
    _tlog(f"T{tier} submission received: {sub_id} (auth_token={token})")

    if has_doi:
        title = "(deferred — resolving from DOI)"
        abstract = ""
        keywords: list = []
        license_id = ""
        creators = [{"name": submitter["name"], "orcid": token}]
        publication_date = _now_iso()[:10]
        resource_type = None
        subject = None
        funding = None
        related_identifiers: list = []
        source = "doi"
        source_ref = doi
    else:
        upload_meta = _validate_upload_metadata({
            "title": form.get("title"),
            "abstract": form.get("abstract"),
            "keywords": form.get("keywords"),
            "license": form.get("license"),
            "resource_type": form.get("resource_type"),
            "publication_date": form.get("publication_date"),
            "subject": form.get("subject"),
            "funding": form.get("funding"),
            "creators": form.get("creators"),
            "related_identifiers": form.get("related_identifiers"),
            "deposit_consent": form.get("deposit_consent"),
        })

        total = 0
        head = b""
        with pdf_path.open("wb") as out:
            while chunk := await pdf.read(64 * 1024):
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    out.close()
                    pdf_path.unlink(missing_ok=True)
                    sub_dir.rmdir()
                    raise HTTPException(413,
                        f"PDF exceeds {MAX_PDF_BYTES // (1024*1024)} MB limit")
                if not head:
                    head = chunk[:5]
                out.write(chunk)
        if not head.startswith(b"%PDF-"):
            pdf_path.unlink(missing_ok=True)
            sub_dir.rmdir()
            raise HTTPException(415, "Uploaded file is not a PDF")

        title = upload_meta["title"]
        abstract = upload_meta["abstract"]
        keywords = upload_meta["keywords"]
        license_id = upload_meta["license"]
        creators = upload_meta["creators"]
        # Strip ORCID from creator entries — none of the creator block
        # gets a real ORCID in test mode either. The submitter's own ORCID
        # is the only one ever sent verified; co-authors are typed.
        # We leave them as-is (they're typed values, not the real auth
        # ORCID) — but the submitter-row ORCID, which the form
        # synchronizes from the readonly verified field, is replaced.
        for c in creators:
            if c.get("orcid") == _normalize_orcid(auth_orcid):
                c["orcid"] = token
        publication_date = upload_meta["publication_date"]
        resource_type = upload_meta["resource_type"]
        subject = upload_meta["subject"]
        funding = upload_meta["funding"]
        related_identifiers = upload_meta["related_identifiers"]
        submitter["deposit_consent"] = upload_meta["deposit_consent"]
        source = "upload"
        source_ref = pdf.filename or "paper.pdf"

    if not has_doi:
        text = ingest.extract_pdf_text(str(pdf_path))
        if len(text) < ingest.PDF_TEXT_MIN_CHARS:
            pdf_path.unlink(missing_ok=True)
            try:
                sub_dir.rmdir()
            except OSError:
                pass
            raise HTTPException(422, {
                "error": "no_text_layer",
                "message": (
                    "The PDF has no extractable text layer "
                    f"({len(text)} chars). ICSAC reviews text-layer PDFs only — "
                    "image-only scans cannot be evaluated by the panel."
                ),
            })
        pdf_size = pdf_path.stat().st_size
        pdf_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    else:
        pdf_size = 0
        pdf_sha = ""

    redacted_form = {**submitter, "orcid": token}
    auth_block = {
        "orcid": token,
        "name_on_record": auth_name or None,
        "verified": bool(auth_orcid),
    }

    received_at = _now_iso()
    submission_record = {
        "sub_id": sub_id,
        "test_mode": True,
        "tier": tier,
        "received_at": received_at,
        "source": source,
        "source_ref": source_ref,
        "form": redacted_form,
        "auth": auth_block,
        "doi": doi if has_doi else None,
        "pending_pdf_fetch": has_doi,
        "title": title,
        "abstract": abstract,
        "keywords": keywords,
        "license": license_id,
        "creators": creators,
        "publication_date": publication_date,
        "resource_type": resource_type,
        "subject": subject,
        "funding": funding,
        "related_identifiers": related_identifiers,
        "pdf": {
            "filename": "paper.pdf",
            "size_bytes": pdf_size,
            "sha256": pdf_sha,
        } if not has_doi else None,
    }
    (sub_dir / "submission.json").write_text(
        json.dumps(submission_record, indent=2)
    )
    (sub_dir / "state.json").write_text(json.dumps({
        "state": "received",
        "test_mode": True,
        "tier": tier,
        "received_at": received_at,
    }, indent=2))

    # Drop a marker into the test queue. The submission worker reads
    # tier from submission.json and applies T2/T3 routing to its
    # side-effect stages.
    TEST_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    (TEST_QUEUE_DIR / sub_id).write_text(received_at)

    _audit_append({
        "sub_id": sub_id,
        "event": "submission_received",
        "tier": tier,
        "source": source,
        "source_ref": source_ref,
        "title": title[:200],
        "license": license_id,
        "pdf_sha256": pdf_sha,
        "pdf_size_bytes": pdf_size,
        "auth_orcid": token,
        "auth_verified": bool(auth_orcid),
    }, test_mode=True)

    _audit_append({
        "sub_id": sub_id,
        "event": "tier_elevated",
        "tier": tier,
        "auth_orcid": token,
    }, test_mode=True)

    # T3 invocation tripwire — fires to the curator the moment T3 is
    # resolved, so the curator knows real frontier tokens + a Zenodo
    # sandbox call are about to fire and can set TEST_TIERS_DISABLED=true
    # + restart if anything looks wrong.
    if tier == 3:
        try:
            notify.send_to_curator(
                f"⚠️ T3 invoked: {token}, at {received_at}. "
                f"Pipeline running. Kill switch: TEST_TIERS_DISABLED=true.",
                parse_mode=None,
            )
        except Exception as exc:
            print(f"T3 tripwire alert failed: {exc}", file=sys.stderr)

    return JSONResponse({
        "sub_id": sub_id,
        "status_url": f"/submission/{sub_id}",
        "state": "received",
        "received_at": received_at,
        "test_mode": True,
        "tier": tier,
    })


# ── endpoints ───────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": _now_iso()}


def _validate_doi_shape(doi: str) -> None:
    """Synchronous shape-only check on a DOI or arXiv reference. The actual
    resolution + PDF fetch is deferred to the worker so the handler
    returns in <1s rather than holding the request open through a multi-
    MB download. Raises HTTPException only on shape errors that the user
    can fix at submit time. Accepted shapes:
      Zenodo DOI : 10.5281/zenodo.NNNNN
      arXiv DOI  : 10.48550/arXiv.YYMM.NNNNN[vN]
      arXiv ID   : YYMM.NNNNN[vN]   (modern format only — pre-2007 IDs
                                     like math.GT/0309136 are out of scope)
    """
    if ZENODO_DOI_RE.match(doi) or ARXIV_REF_RE.match(doi):
        return
    if DOI_SHAPE_RE.match(doi):
        raise HTTPException(422, {
            "error": "doi_unsupported",
            "message": (
                "DOI mode supports Zenodo (10.5281/zenodo.NNNNN) and arXiv "
                "(10.48550/arXiv.YYMM.NNNNN, or a bare arXiv ID like "
                "2103.12345). Other DOI sources (Crossref, DataCite, "
                "publisher DOIs) are a known gap — for those, upload the "
                "PDF directly using the 'Upload PDF' tab."
            ),
        })
    raise HTTPException(422, {
        "error": "doi_invalid",
        "message": (
            "That doesn't look like a Zenodo DOI, an arXiv DOI, or an "
            "arXiv ID. Expected forms: 10.5281/zenodo.NNNNN, "
            "10.48550/arXiv.YYMM.NNNNN, or YYMM.NNNNN. If you don't "
            "have any of those, switch to the 'Upload PDF' tab."
        ),
    })


@app.post("/api/submit")
async def api_submit(request: Request):
    # Read raw body once; verify HMAC over the bytes the proxy signed;
    # then let Starlette parse the cached body as multipart. We can't
    # use FastAPI's UploadFile/Form parameter sugar — that consumes
    # the stream before we can hash it.
    raw_body = await request.body()
    if len(raw_body) > MAX_PDF_BYTES + 64 * 1024:
        raise HTTPException(413,
            f"request body exceeds {MAX_PDF_BYTES // (1024*1024)} MB cap")
    _verify_hmac(request, raw_body)

    form = await request.form()

    submitter = _validate_submitter({
        "name": form.get("name"),
        "email": form.get("email"),
        "orcid": form.get("orcid", ""),
        "coi": form.get("coi", ""),
    })

    # Verified-identity headers from the CF Pages auth gate. We need these
    # early so the test-mode short-circuit can fire BEFORE any counter
    # allocation, sub_dir creation, PDF write, ingest, or notify call —
    # the test path must not touch real production state.
    auth_orcid = (request.headers.get("x-icsac-auth-orcid") or "").strip()
    auth_name_enc = (request.headers.get("x-icsac-auth-name") or "").strip()
    auth_name = unquote(auth_name_enc) if auth_name_enc else ""

    # Tier resolution (test ORCIDs only). Production ORCIDs hard-pin to
    # tier 1, the X-ICSAC-Test-Tier header is ignored, and the production
    # flow below runs unchanged.
    tier_header = (request.headers.get("x-icsac-test-tier") or "").strip()
    tier = _resolve_tier(auth_orcid, tier_header)

    if is_test_submission(auth_orcid):
        # Tier 1: canned ACCEPT short-circuit; skip panel/RQC/Zenodo/
        # email/Telegram entirely. The submission record is stamped
        # tier=1 + test_mode=true and audit entries carry test:true so
        # stats and brain aggregators can filter.
        # Tier 2/3: real pipeline (panel + RQC + decision logic) with
        # side-effect routing isolated to the test subtree — handled
        # in handle_test_pipeline_submission below.
        if tier == 1:
            raw_doi = form.get("doi", "") or ""
            doi_for_test = _normalize_doi(raw_doi if isinstance(raw_doi, str) else "")
            pdf_field = form.get("pdf")
            title = (form.get("title") or "").strip() or (
                "(test mode — DOI submission)"
                if doi_for_test
                else "(test mode — PDF submission)"
            )
            if doi_for_test:
                test_source, test_source_ref = "doi", doi_for_test
            elif pdf_field is not None and getattr(pdf_field, "filename", ""):
                test_source = "upload"
                test_source_ref = pdf_field.filename or "paper.pdf"
            else:
                test_source, test_source_ref = "test", "synthetic"
            if tier_header and not _tiers_disabled():
                # Submitter sent a tier header but it didn't resolve to
                # T2/T3 (garbage, unknown, or disabled). Audit the
                # downgrade so the trail shows what was attempted.
                _audit_append({
                    "event": "tier_downgrade",
                    "tier_requested": tier_header[:8],
                    "tier_resolved": 1,
                    "auth_orcid": _test_token(1),
                }, test_mode=True)
            return handle_test_submission(
                submitter=submitter, auth_orcid=auth_orcid,
                auth_name=auth_name, source=test_source,
                source_ref=test_source_ref, title=title[:400],
                received_at=_now_iso(),
            )
        # Tier 2 or Tier 3 — real pipeline, test subtree.
        return await handle_test_pipeline_submission(
            tier=tier, request=request, form=form, submitter=submitter,
            auth_orcid=auth_orcid, auth_name=auth_name,
        )

    pdf = form.get("pdf")
    has_pdf = pdf is not None and hasattr(pdf, "read") and getattr(pdf, "filename", "")
    raw_doi = form.get("doi", "") or ""
    doi = _normalize_doi(raw_doi if isinstance(raw_doi, str) else "")
    has_doi = bool(doi)

    if has_doi and has_pdf:
        raise HTTPException(400, {
            "error": "doi_xor_pdf",
            "message": "Provide a DOI OR a PDF upload, not both.",
        })
    if not has_doi and not has_pdf:
        raise HTTPException(400, {
            "error": "doi_or_pdf_required",
            "message": "Provide a DOI or upload a PDF.",
        })

    if has_doi:
        # Validate shape only — defer the actual Zenodo fetch + PDF
        # download to the worker. Lets the handler return in <1s instead
        # of holding the request open for multi-MB downloads (which
        # previously blew CF Pages' upstream-fetch timeout and blocked
        # the form's redirect on success).
        _validate_doi_shape(doi)

    sub_id = _allocate_sub_id()
    sub_dir = SUBMISSIONS_ROOT / sub_id
    sub_dir.mkdir(parents=True, exist_ok=False)
    pdf_path = sub_dir / "paper.pdf"

    if has_doi:
        # DOI mode: write a stub submission.json with pending_pdf_fetch=true.
        # Worker will resolve metadata + download PDF + run text-layer check
        # on its own time. If anything fails there, worker emails the author.
        # Deposit-metadata fields (resource_type, subject, funding,
        # related_identifiers) stay null on the DOI route — the existing DOI
        # already has its own metadata and ICSAC is not minting a new one;
        # the deposit step is upload-only.
        title = "(deferred — resolving from DOI)"
        abstract = ""
        keywords: list = []
        license_id = ""
        creators = [{"name": submitter["name"], "orcid": submitter["orcid"]}]
        publication_date = _now_iso()[:10]
        resource_type = None
        subject = None
        funding = None
        related_identifiers: list = []
        source = "doi"
        source_ref = doi
    else:
        # Upload path — submitter-supplied metadata is authoritative.
        upload_meta = _validate_upload_metadata({
            "title": form.get("title"),
            "abstract": form.get("abstract"),
            "keywords": form.get("keywords"),
            "license": form.get("license"),
            "resource_type": form.get("resource_type"),
            "publication_date": form.get("publication_date"),
            "subject": form.get("subject"),
            "funding": form.get("funding"),
            "creators": form.get("creators"),
            "related_identifiers": form.get("related_identifiers"),
            "deposit_consent": form.get("deposit_consent"),
        })

        total = 0
        head = b""
        with pdf_path.open("wb") as out:
            while chunk := await pdf.read(64 * 1024):
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    out.close()
                    pdf_path.unlink(missing_ok=True)
                    sub_dir.rmdir()
                    raise HTTPException(413,
                        f"PDF exceeds {MAX_PDF_BYTES // (1024*1024)} MB limit")
                if not head:
                    head = chunk[:5]
                out.write(chunk)
        if not head.startswith(b"%PDF-"):
            pdf_path.unlink(missing_ok=True)
            sub_dir.rmdir()
            raise HTTPException(415, "Uploaded file is not a PDF")

        title = upload_meta["title"]
        abstract = upload_meta["abstract"]
        keywords = upload_meta["keywords"]
        license_id = upload_meta["license"]
        creators = upload_meta["creators"]
        publication_date = upload_meta["publication_date"]
        resource_type = upload_meta["resource_type"]
        subject = upload_meta["subject"]
        funding = upload_meta["funding"]
        related_identifiers = upload_meta["related_identifiers"]
        # Promote deposit_consent into the form key alongside coi/exclusivity
        # so the persisted submission.json keeps all three opt-in confirmations
        # together — the deposit step gates on this on accept.
        submitter["deposit_consent"] = upload_meta["deposit_consent"]
        source = "upload"
        source_ref = pdf.filename or "paper.pdf"

    # Text-layer check — only on upload mode (PDF is on disk now). DOI
    # mode defers this to the worker, since the PDF doesn't exist yet.
    if not has_doi:
        text = ingest.extract_pdf_text(str(pdf_path))
        if len(text) < ingest.PDF_TEXT_MIN_CHARS:
            pdf_path.unlink(missing_ok=True)
            try:
                sub_dir.rmdir()
            except OSError:
                pass
            raise HTTPException(422, {
                "error": "no_text_layer",
                "message": (
                    "The PDF has no extractable text layer "
                    f"({len(text)} chars). ICSAC reviews text-layer PDFs only — "
                    "image-only scans cannot be evaluated by the panel."
                ),
            })
        pdf_size = pdf_path.stat().st_size
        pdf_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    else:
        pdf_size = 0
        pdf_sha = ""

    # Verified-identity headers from CF Pages auth gate. (Already extracted
    # earlier for the test-mode check; stamping the auth block alongside
    # the form block lets the audit record show what came from ORCID's
    # record vs what the submitter typed — name in particular is editable
    # for citation and can legitimately differ from the ORCID-record name.)
    auth_block = {
        "orcid": auth_orcid or None,
        "name_on_record": auth_name or None,
        "verified": bool(auth_orcid),
    }

    received_at = _now_iso()
    submission_record = {
        "sub_id": sub_id,
        "received_at": received_at,
        "source": source,
        "source_ref": source_ref,
        "form": submitter,
        "auth": auth_block,
        "doi": doi if has_doi else None,
        "pending_pdf_fetch": has_doi,  # worker will resolve + fill metadata
        "title": title,
        "abstract": abstract,
        "keywords": keywords,
        "license": license_id,
        "creators": creators,
        "publication_date": publication_date,
        "resource_type": resource_type,
        "subject": subject,
        "funding": funding,
        "related_identifiers": related_identifiers,
        "pdf": {
            "filename": "paper.pdf",
            "size_bytes": pdf_size,
            "sha256": pdf_sha,
        } if not has_doi else None,
    }
    (sub_dir / "submission.json").write_text(
        json.dumps(submission_record, indent=2)
    )
    (sub_dir / "state.json").write_text(
        json.dumps({"state": "received", "received_at": received_at}, indent=2)
    )

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    (QUEUE_DIR / sub_id).write_text(received_at)

    _audit_append({
        "sub_id": sub_id,
        "event": "submission_received",
        "source": source,
        "source_ref": source_ref,
        "title": title[:200],
        "license": license_id,
        "pdf_sha256": pdf_sha,
        "pdf_size_bytes": pdf_size,
        "auth_orcid": auth_orcid or None,
        "auth_verified": bool(auth_orcid),
    })

    # Send the "received" email NOW only when we have a real title — i.e.
    # upload route (form-supplied). On DOI route the title is "(deferred —
    # resolving from DOI)" until the worker fetches metadata, so the
    # received-email firing moves to submission_worker._resolve_pending_doi
    # success path. Keeps the author from getting a confirmation email
    # with the placeholder title.
    if not has_doi:
        try:
            notify_author.send_received(
                to=submitter["email"], sub_id=sub_id,
                title=title, author_name=submitter["name"],
            )
        except Exception as exc:
            print(f"received email failed (non-fatal): {exc}", file=sys.stderr)
            _audit_append({"sub_id": sub_id, "event": "received_email_failed",
                           "error": str(exc)[:200]})

    try:
        notify.send_to_curator(
            f"ICSAC submission received\n\n"
            f"ID: {sub_id}\n"
            f"Source: {source} ({source_ref})\n"
            f"Title: {title[:120]}\n"
            f"Submitter: {submitter['name']} <{submitter['email']}>\n"
            f"License: {license_id or '(from DOI)'}\n"
            f"Status: queued for panel review",
            parse_mode=None,
        )
    except Exception:
        pass

    try:
        notify.send_ntfy(f"{sub_id}: {title[:120]}",
                         title="ICSAC submission received")
    except Exception:
        pass

    return JSONResponse({
        "sub_id": sub_id,
        "status_url": f"/submission/{sub_id}",
        "state": "received",
        "received_at": received_at,
    })


@app.get("/api/submission/{sub_id}/state")
def api_submission_state(sub_id: str):
    if not (SUB_ID_RE.match(sub_id) or TEST_SUB_ID_RE.match(sub_id)):
        raise HTTPException(400, "bad submission id")
    # Test submissions live under SUBMISSIONS_ROOT/test/; production under
    # SUBMISSIONS_ROOT/. Resolve based on id shape.
    is_test = TEST_SUB_ID_RE.match(sub_id) is not None
    state_path = (TEST_SUBMISSIONS_ROOT if is_test else SUBMISSIONS_ROOT) / sub_id / "state.json"
    if not state_path.exists():
        raise HTTPException(404, "no such submission")
    data = json.loads(state_path.read_text())
    # Filter — never expose internals (panel scores, RQC, file paths).
    # Storage stays UTC ISO; we emit a parallel display field formatted
    # as 'MM/DD/YY HH:MM:SS EDT|EST' for the state page. Clients that
    # need the raw value can still read *_at; humans get *_at_display.
    received_utc = data.get("received_at")
    completed_utc = data.get("completed_at")
    return {
        "sub_id": sub_id,
        "state": data.get("state", "unknown"),
        "received_at": received_utc,
        "received_at_display": to_et_display(received_utc),
        "completed_at": completed_utc,
        "completed_at_display": to_et_display(completed_utc),
        "decision": data.get("decision"),
        "test_mode": bool(data.get("test_mode", False)),
        "tier": data.get("tier") if data.get("test_mode") else None,
    }


@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    body = detail if isinstance(detail, dict) else {"error": detail}
    return JSONResponse(body, status_code=exc.status_code)
