"""
alerts.py — push the events that need a human to the owner's phone.

Uses ntfy.sh: install the ntfy app, subscribe to CONFIG["alert_ntfy_topic"],
and every alert below arrives as a push notification. The topic name is the
only access control, so it is a long random string — anyone who knows it can
read the alerts. Keep it out of anything shared.

What alerts, and why each one needs a human:
  * kill switch fired              — no more trades today
  * equity floor tripped           — trading halted until you reset it
  * positions still open after the close — something did not flatten
  * any log line asking for manual action ("CHECK MANUALLY", "manual
    intervention") — caught by AlertLogHandler, so future messages of that
    kind alert without wiring
  * main-loop crash, agent start  — restarts are worth knowing about
  * daily summary                  — low priority, one per day

Sending never blocks trading and never raises: it runs on a daemon thread with
a short timeout, and identical alerts are suppressed for DEDUPE_SECS so a 30s
monitor loop cannot spam the phone.

_sandbox.py sets OPTIONSTRADER_ALERTS=0, so the offline tests never send.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests

log = logging.getLogger("OptionsAgent")

DEDUPE_SECS = 1800
_last_sent: dict[str, float] = {}
_lock = threading.Lock()

# Log text that means "a human has to look at Tradier now".
MANUAL_MARKERS = ("CHECK MANUALLY", "CHECK TRADIER MANUALLY", "manual intervention")


def _config() -> dict:
    try:
        from config import CONFIG
        return CONFIG
    except Exception:
        return {}


def enabled() -> bool:
    if os.environ.get("OPTIONSTRADER_ALERTS", "1") == "0":
        return False
    cfg = _config()
    return bool(cfg.get("alerts_enabled", False) and cfg.get("alert_ntfy_topic"))


def send(title: str, message: str, priority: str = "default",
         tags: str = "") -> bool:
    """Queue one push notification. Returns False if disabled or deduped.

    priority: min, low, default, high, urgent (ntfy's scale).
    """
    if not enabled():
        return False
    key = f"{title}|{message}"
    now = time.time()
    with _lock:
        if now - _last_sent.get(key, 0) < DEDUPE_SECS:
            return False
        _last_sent[key] = now

    cfg = _config()
    url = f"{cfg.get('alert_ntfy_server', 'https://ntfy.sh').rstrip('/')}/{cfg['alert_ntfy_topic']}"
    headers = {"Title": title.encode("utf-8"), "Priority": priority}
    if tags:
        headers["Tags"] = tags

    def _post():
        try:
            requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=5)
        except Exception as e:
            # Never through log.error: the AlertLogHandler would loop on it.
            log.warning(f"alert not delivered ({e})")

    threading.Thread(target=_post, daemon=True).start()
    return True


class AlertLogHandler(logging.Handler):
    """Forwards any WARNING+ log line that asks for manual action."""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if any(m in msg for m in MANUAL_MARKERS):
                send("OptionsAgent: check Tradier now", msg[:400],
                     priority="urgent", tags="rotating_light")
        except Exception:
            pass


__all__ = ["send", "enabled", "AlertLogHandler", "MANUAL_MARKERS"]
