"""Single-attempt legacy trigger bridge with a dedicated credential file.

A transport failure is ambiguous: remote state must be checked before replay.
The private legacy .env and Docker credential inspection are not used.
"""

from __future__ import annotations
from qsim.services.mutation_lock import serialized_mutation


import logging
import os
import subprocess
from pathlib import Path

import requests

logger = logging.getLogger("qsim.legacy_trigger")

LEGACY_AUTH_TOKEN_FILE = Path(
    os.environ.get("QSIM_LEGACY_AUTH_TOKEN_FILE", str(Path(__file__).resolve().parents[2] / ".secrets" / "legacy-auth"))
)
LEGACY_TRIGGER_FALLBACK = "http://127.0.0.1:8502"


def _auth_token() -> str:
    try:
        for line in LEGACY_AUTH_TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "QUANT_AUTH_TOKEN":
                return value.strip().strip("\"'")
    except OSError as exc:
        raise RuntimeError("legacy trigger credential is unavailable") from exc
    raise RuntimeError("legacy trigger credential is unavailable")


def _trigger_urls() -> list[str]:
    configured = os.environ.get("QSIM_LEGACY_TRIGGER", "").strip()
    if configured:
        return [configured.rstrip("/")]
    candidates: list[str] = []
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
             "quant-sim-trigger"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        for ip in proc.stdout.split():
            candidates.append(f"http://{ip}:8502")
    except Exception:
        pass
    candidates.append(LEGACY_TRIGGER_FALLBACK)
    return list(dict.fromkeys(candidates))


@serialized_mutation
def call(name: str, *, timeout: int = 600) -> dict:
    """POST /{name} to the legacy trigger; returns its JSON body."""
    headers = {"X-Auth-Token": _auth_token()}
    # One POST only: a timeout/reset may occur after the remote task committed.
    # Replaying it on another address cannot be assumed idempotent.
    base_url = _trigger_urls()[0]
    try:
        resp = requests.post(f"{base_url}/{name}", headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        raise RuntimeError(f"legacy trigger {name} failed; verify remote state before retry") from None
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"legacy trigger {name} returned invalid JSON") from exc
