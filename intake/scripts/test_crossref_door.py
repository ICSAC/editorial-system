#!/usr/bin/env python3
"""Crossref requests go through one door: bounded concurrency, spaced searches,
and a retry for a refused request.

Offline: urllib's urlopen is replaced by a fake Crossref that refuses (HTTP
429, no Retry-After) any request beyond three in flight, as the real API
does. Nothing is written. Run from the repo root:
    .venv/bin/python intake/scripts/test_crossref_door.py

The citation check verifies references from eight threads. Against the real
API a burst of eight refused about half, so a reference whose DOI resolves
fine lost its confirmation at random.
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


import submission_intake as si  # noqa: E402

si.CROSSREF_RETRY_WAITS = (0.01, 0.01)
si.CROSSREF_SEARCH_MIN_GAP = 0.05


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _work(doi: str) -> dict:
    return {"message": {"DOI": doi, "title": [f"A record for {doi}"], "type": "journal-article",
                        "author": [{"given": "Ada", "family": "Author"}], "issued": {"date-parts": [[2020]]}}}


class FakeCrossref:
    """Refuses above `limit` requests in flight; `script` maps a URL fragment
    to a list of outcomes served in order ("429", "404", "timeout", "ok")."""

    def __init__(self, limit=3, hold=0.05):
        self.limit, self.hold = limit, hold
        self.lock = threading.Lock()
        self.inflight = self.peak = self.calls = self.refused = 0
        self.script: dict[str, list[str]] = {}
        self.search_starts: list[float] = []

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        with self.lock:
            self.calls += 1
            if "query.bibliographic" in url:
                self.search_starts.append(time.monotonic())
            outcome = "ok"
            for frag, seq in self.script.items():
                if frag in url and seq:
                    outcome = seq.pop(0)
                    break
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            over = self.inflight > self.limit
        try:
            time.sleep(self.hold)
            if over or outcome == "429":
                with self.lock:
                    self.refused += 1
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
            if outcome == "404":
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
            if outcome == "timeout":
                raise TimeoutError("timed out")
            if "query.bibliographic" in url:
                return _Resp({"message": {"items": [_work("10.0000/s.1")["message"]]}})
            return _Resp(_work(url.rsplit("/works/", 1)[-1]))
        finally:
            with self.lock:
                self.inflight -= 1


def install(fake):
    urllib.request.urlopen = fake
    return fake


real_urlopen = urllib.request.urlopen
try:
    print("1. eight threads against a Crossref that refuses above three in flight")
    fake = install(FakeCrossref())
    dois = [f"10.0000/door.{i}" for i in range(16)]
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(si.fetch_crossref_metadata, dois))
    lost = [d for d, g in zip(dois, got) if not g]
    check(not lost, f"every DOI lookup returns its record ({len(lost)} of {len(dois)} lost)")
    check(fake.peak <= getattr(si, "CROSSREF_MAX_CONCURRENT", 99) and fake.peak <= 3,
          f"at most {getattr(si, 'CROSSREF_MAX_CONCURRENT', '?')} requests in flight (saw {fake.peak})")

    print("2. a refused request is tried again")
    fake = install(FakeCrossref())
    fake.script = {"door.r1": ["429"]}
    rec = si.fetch_crossref_metadata("10.0000/door.r1")
    check(bool(rec) and fake.calls == 2, f"429 then 200 returns the record (calls {fake.calls})")
    fake = install(FakeCrossref())
    fake.script = {"door.t1": ["timeout"]}
    rec = si.fetch_crossref_metadata("10.0000/door.t1")
    check(bool(rec), "a timeout then 200 returns the record")

    print("3. a missing DOI is an answer, not a refusal")
    fake = install(FakeCrossref())
    fake.script = {"door.missing": ["404"]}
    check(si.fetch_crossref_metadata("10.0000/door.missing") is None and fake.calls == 1,
          f"404 returns None at once (calls {fake.calls})")

    print("4. a request refused every time still fails honestly")
    fake = install(FakeCrossref())
    fake.script = {"door.down": ["429", "429", "429", "429"]}
    check(si.fetch_crossref_metadata("10.0000/door.down") is None, "a lookup refused on every try returns None")
    fake.script = {"query.bibliographic": ["429", "429", "429", "429"]}
    try:
        si.search_crossref_bibliographic("Author, A. (2020). A long enough reference string.", raise_errors=True)
        raised = False
    except urllib.error.HTTPError:
        raised = True
    check(raised, "a search refused on every try raises when asked to")
    fake.script = {"query.bibliographic": ["429", "429", "429", "429"]}
    check(si.search_crossref_bibliographic("Author, A. (2020). A long enough reference string.") == [],
          "and returns [] by default, as before")

    print("5. searches are spaced")
    fake = install(FakeCrossref(hold=0.0))
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda i: si.search_crossref_bibliographic(f"Author, A. (2020). Reference string number {i}."),
                    range(4)))
    gaps = [b - a for a, b in zip(sorted(fake.search_starts), sorted(fake.search_starts)[1:])]
    check(len(gaps) == 3 and min(gaps) >= 0.045,
          f"search starts are at least the minimum gap apart (min gap {min(gaps) if gaps else 0:.3f}s)")
finally:
    urllib.request.urlopen = real_urlopen

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("ALL PASS")
