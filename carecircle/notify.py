"""Outbound channels. Only free, account-less services: ntfy.sh push (optional) or a local outbox."""
from __future__ import annotations

import os

import httpx


def send_caregiver(recipient: str, channel: str, text: str, title: str = "CareCircle") -> tuple[str, str]:
    """Returns (status, detail). Never raises."""
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if channel == "ntfy" and topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            r = httpx.post(f"{server}/{topic}", content=text.encode(), timeout=10,
                           headers={"Title": title, "Tags": "house_with_garden", "Priority": "default"})
            if r.status_code < 300:
                msg_id = r.json().get("id", "?") if r.headers.get("content-type", "").startswith("application/json") else "?"
                return "delivered", f"ntfy.sh topic '{topic}' accepted it (HTTP {r.status_code}, id {msg_id})"
            return "failed", f"ntfy.sh returned HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            return "failed", f"ntfy.sh unreachable: {type(exc).__name__}"
    return "queued", "Saved to the local outbox (set NTFY_TOPIC to push to a phone via ntfy.sh)"
