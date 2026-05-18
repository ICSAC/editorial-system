"""Notification helpers for the editorial workflow.

The institute's deployment fans curator-facing alerts out to Telegram and
ntfy. Workflow code should call `send_to_curator` rather than naming a
specific channel; forks that wire other transports (Slack, Matrix, email,
generic webhook) can replace this module's body without touching the
calling code. `send_telegram` and `send_ntfy` remain available as
channel-specific adapters for callers that legitimately need a specific
transport (e.g. the Telegram conversation responder for inbound replies,
which is platform-specific by nature).
"""

import json
import re
import urllib.request
import urllib.error

import config


def send_telegram(message: str, parse_mode: str | None = "Markdown",
                  chat_override: str | None = None,
                  thread_override: str | None = None) -> int | None:
    """Send a message via Telegram bot API. Pass parse_mode=None to send as plain text.

    Returns the Telegram message_id on success (an int), None on failure.

    Callers that only care about success/failure can `if send_telegram(...):` —
    `None` is falsy and ints from Telegram are always truthy. Callers that
    need the message_id (e.g. the submission borderline escalation, which
    writes the id into the responder's incident JSON for reply-to lookup)
    get the real value instead of the bool subclass-of-int that the prior
    return shape silently produced.

    Routes every editorial-system message to the ICSAC forum topic when
    TELEGRAM_THREAD_ID is set in the env. The bot + supergroup are
    shared with other operator monitoring topics, so the thread id is what
    keeps the ICSAC traffic segregated.

    `chat_override` (Tier 3 test path): when set non-empty, sends to that
    chat ID instead of config.TELEGRAM_CHAT_ID. The thread id behavior is
    unchanged so a test chat that lives in the same supergroup can still
    pin to its own topic.
    """
    if (chat_override or "") == "__suppress__":
        return None
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    chat_id = (chat_override or "").strip() or config.TELEGRAM_CHAT_ID
    payload = {
        "chat_id": chat_id,
        "text": message,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    thread_id = (thread_override or "").strip() or getattr(config, "TELEGRAM_THREAD_ID", "")
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            print(f"  Telegram warning: ignoring non-integer thread id {thread_id!r}")
    data = json.dumps(payload).encode()

    req = urllib.request.Request(url, data=data)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return None
            try:
                body = json.loads(resp.read().decode())
            except (ValueError, UnicodeDecodeError):
                return None
            if not body.get("ok"):
                return None
            msg_id = body.get("result", {}).get("message_id")
            return int(msg_id) if isinstance(msg_id, (int, str)) and str(msg_id).lstrip("-").isdigit() else None
    except urllib.error.URLError as e:
        print(f"  Telegram error: {e}")
        return None


def send_to_curator(message: str, parse_mode: str | None = "Markdown",
                    chat_override: str | None = None,
                    thread_override: str | None = None,
                    ntfy: bool = True) -> bool:
    """Dispatch a curator-facing alert to the operator's configured channels.

    In the institute's deployment this fans out to Telegram + ntfy. Forks
    that wire other channels (Slack, Matrix, email, webhook) should swap
    this function's body; the workflow only calls send_to_curator and
    does not assume which channels are live.

    Test-tier routing: pass chat_override (and optionally thread_override)
    to redirect Telegram to a separate test chat/topic. Pass ntfy=False to
    skip the ntfy backup so test runs don't fire pain alerts.
    """
    tg_ok = bool(send_telegram(message, parse_mode=parse_mode,
                               chat_override=chat_override,
                               thread_override=thread_override))
    # ntfy doesn't render markdown; strip the formatting for the backup
    # channel. The Telegram message_id from send_telegram is the
    # caller-relevant return value for conversation-thread tracking,
    # but send_to_curator is for one-shot alerts that don't need it.
    if ntfy:
        try:
            plain = re.sub(r"[*_`]", "", message)
            send_ntfy(plain, title="ICSAC")
        except Exception:
            pass
    return tg_ok


def send_ntfy(message: str, title: str = "ICSAC Review Pipeline") -> bool:
    """Send notification to ntfy backup channel.

    Short-circuits to False (no-op) when NTFY_BACKUPS_URL is unset, matching
    the pattern used by fire_pain() in editorial_workflow — the ntfy channel
    is optional and a missing URL should not be a fatal config error.
    """
    url = getattr(config, "NTFY_BACKUPS_URL", "")
    if not url:
        return False
    req = urllib.request.Request(url, data=message.encode())
    req.add_header("Title", title)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.URLError as e:
        print(f"  ntfy error: {e}")
        return False


def notify_review_complete(review_data: dict, aggregate: dict) -> None:
    """Send review completion notification via Telegram and ntfy."""
    title = review_data.get("title", "Untitled")
    doi = review_data.get("doi", "N/A")
    rec = aggregate.get("recommendation", "REVIEW_FURTHER")
    models = ", ".join(aggregate.get("models_used", ["unknown"]))
    disagreement = aggregate.get("disagreement", False)

    msg = (
        f"*ICSAC Review Complete*\n\n"
        f"*Title:* {title}\n"
        f"*DOI:* {doi}\n"
        f"*Recommendation:* {rec}\n"
        f"*Models:* {models}\n"
        f"*Disagreement:* {'Yes' if disagreement else 'No'}\n\n"
        f"Review saved. Awaiting human curator decision."
    )

    send_telegram(msg)
    send_ntfy(f"{title}\nDOI: {doi}\nRecommendation: {rec}", title="ICSAC Review")



def alert_panel_failure(review_data: dict, reviews: list[dict],
                        valid_count: int, total_slots: int,
                        min_required: int) -> None:
    """AI panel review fell below minimum threshold after self-heal retries.

    Sends Telegram (operator) + ntfy /pain (monitoring). The submission is
    NOT auto-processed — it stays pending in Zenodo for human attention.
    """
    title = review_data.get("title", "Untitled")
    doi = review_data.get("doi", "N/A")
    failed = [r.get("model", "?") for r in reviews if "error" in r]
    succeeded = [r.get("model", "?") for r in reviews if "error" not in r]
    errors = []
    for r in reviews:
        if "error" in r:
            err = r["error"][:120]
            errors.append(f"  - {r.get('model', '?')}: {err}")
    err_block = "\n".join(errors) if errors else "  (no error details)"

    msg = (
        f"ICSAC Pipeline — AI Panel Failure\n\n"
        f"Paper: {title}\n"
        f"DOI: {doi}\n"
        f"Reviewers OK: {valid_count}/{total_slots} (min required: {min_required})\n"
        f"Succeeded: {', '.join(succeeded) or 'none'}\n"
        f"Failed: {', '.join(failed)}\n\n"
        f"Errors:\n{err_block}\n\n"
        f"Self-heal retries exhausted. Submission paused — needs human attention. "
        f"Zenodo request remains pending."
    )

    send_telegram(msg, parse_mode=None)

    # Pain signal direct to monitoring endpoint
    url = getattr(config, "NTFY_PAIN_URL", "")
    if url:
        import urllib.request
        try:
            req = urllib.request.Request(
                url,
                data=f"AI panel failed for {title}: {valid_count}/{total_slots} reviewers ok".encode(),
            )
            req.add_header("Title", "ICSAC Pipeline: AI Panel Failure")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
