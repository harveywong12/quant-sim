"""Best-effort push notifications via ntfy.

Reads QSIM_NTFY_URL from the environment; silently no-ops when unset so
local runs and tests never fail on missing config. Delivery failures are
logged and swallowed: alerting must never break the trading path.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("qsim.notify")

DEFAULT_TIMEOUT = 8


def notify(title: str, message: str) -> bool:
    url = os.environ.get("QSIM_NTFY_URL", "").strip()
    if not url:
        return False
    body = f"{title}\n{message}" if title else message
    try:
        resp = requests.post(url, data=body.encode("utf-8"), timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("ntfy push failed: %s", exc)
        return False
