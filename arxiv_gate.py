"""The one door to arXiv's legacy API (export.arxiv.org/api).

arXiv's terms (https://info.arxiv.org/help/api/tou.html, read 2026-09-29):
"make no more than one request every three seconds, and limit requests to a
single connection at a time", applied to "all of the machines under your
control as a whole". Every box on the fleet leaves through one public IP, so
that is one budget for all of them.

Every call to the API goes through get(): a lock shared by every process and
thread on this host holds the single connection, calls start at least
MIN_INTERVAL seconds apart, a 429 or 503 is retried after arXiv's Retry-After
(or a backoff), and after a refusal or a stall the door stays shut for
COOL_OFF seconds (Closed is raised at once) so one bad hour of arXiv cannot
stretch every panel run by minutes. Metadata comes from DataCite first (see
submission_intake.fetch_arxiv_metadata); this door is the fallback and the
citation title search.
"""
from __future__ import annotations

import email.utils
import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

MIN_INTERVAL = 3.1       # seconds between request starts (arXiv: one every three)
COOL_OFF = 600           # seconds to stop asking after arXiv refuses or stalls
MAX_RETRY_AFTER = 3600
USER_AGENT = "ICSAC-pipeline/1.0 (https://icsacinstitute.org; mailto:help@icsacinstitute.org)"


class Closed(RuntimeError):
    """arXiv refused or stalled recently; not asking again until the cool-off ends."""


def _paths() -> tuple[Path, Path]:
    d = Path(os.environ.get("ARXIV_GATE_DIR") or (Path.home() / ".cache" / "icsac"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "arxiv-api.lock", d / "arxiv-api.json"


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write(p: Path, st: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, p)


def _retry_after(err: urllib.error.HTTPError) -> float | None:
    raw = (err.headers or {}).get("Retry-After") if err.headers else None
    if not raw:
        return None
    try:
        return min(float(raw), MAX_RETRY_AFTER)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw).timestamp()
            return min(max(0.0, when - time.time()), MAX_RETRY_AFTER)
        except Exception:
            return None


def status() -> dict:
    """The door's state: last request time and, when shut, until when."""
    return _read(_paths()[1])


def get(url: str, *, timeout: float = 20.0, attempts: int = 2) -> bytes:
    """GET an export.arxiv.org API URL within arXiv's limits. Returns the body.
    Raises Closed while cooling off, HTTPError for other HTTP errors, or the
    last network error once the attempts are spent (and shuts the door)."""
    lock_p, state_p = _paths()
    last_err: Exception | None = None
    for attempt in range(attempts):
        backoff = 10.0 * (attempt + 1)
        fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)          # the single connection
            st = _read(state_p)
            now = time.time()
            if st.get("closed_until", 0) > now:
                raise Closed(f"arXiv API cooling off for {int(st['closed_until'] - now)} s more")
            wait = st.get("last", 0) + MIN_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
            st["last"] = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read()
                st.pop("closed_until", None)
                _write(state_p, st)
                return body
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503):
                    _write(state_p, st)
                    raise
                last_err = e
                ra = _retry_after(e)
                if ra is not None:
                    backoff = ra
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
            if attempt == attempts - 1:
                st["closed_until"] = time.time() + max(COOL_OFF, backoff)
            _write(state_p, st)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        if attempt < attempts - 1:
            time.sleep(backoff)
    raise last_err  # type: ignore[misc]
