"""Ring adapter.

Three ways events reach CareCircle, all funnelled through the same normalizer:

* ``LiveRingClient``   - polls the Ring Partner API (``GET /v1/devices``,
  ``GET /v1/history/devices/{id}/events``) with a token from the Ring Developer
  Playground or OAuth account linking (``RING_ACCESS_TOKEN``).
* ``/ring/webhook``    - receives Ring webhook v1.1 payloads, verified with the
  documented HMAC-SHA256 ``X-Signature`` scheme (``RING_WEBHOOK_SECRET``).
* ``SampleRingSource`` - replays ``fixtures/ring_sample_events.json``: events the
  CareCircle author wrote in the documented API shape. Every event from it is
  stored with ``source='sample'`` and labelled SAMPLE in every UI and answer.

Shapes follow https://developer.amazon.com/docs/ring/api-documentation.html
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

RING_API = "https://api.amazonvision.com"
WEBHOOK_TYPE_MAP = {"motion_detected": "motion", "button_press": "ding"}
SOURCE_LABELS = {
    "sample": "SAMPLE event (authored fixture, not from Ring)",
    "ring_api": "Ring Partner API (event history)",
    "ring_webhook": "Ring webhook (HMAC verified)",
}


@dataclass
class Device:
    id: str
    name: str
    room: str
    indoor: bool


class DeviceDirectory:
    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self._by_id = {d["id"]: Device(d["id"], d["name"], d["room"], bool(d["indoor"])) for d in devices}

    def get(self, device_id: str, fallback_name: str | None = None) -> Device:
        if device_id in self._by_id:
            return self._by_id[device_id]
        # Unknown live device: treat as indoor camera named by Ring.
        return Device(device_id, fallback_name or device_id, (fallback_name or "unknown room").lower(), True)

    def learn(self, device_id: str, name: str) -> None:
        if device_id not in self._by_id:
            room = name.lower().replace(" camera", "").replace(" cam", "")
            indoor = not any(w in room for w in ("front", "door", "porch", "drive", "garage"))
            self._by_id[device_id] = Device(device_id, name, room, indoor)

    def all(self) -> list[Device]:
        return list(self._by_id.values())


def normalize_history_event(item: dict[str, Any], directory: DeviceDirectory, source: str) -> dict[str, Any]:
    """Ring ``history-events`` JSON:API item -> CareCircle event row."""
    attrs = item.get("attributes", {})
    raw_type = attrs.get("event_type", "")
    event_type, _, sub = raw_type.partition(".")
    sub_type = attrs.get("sub_type") or (sub or None)
    device_id = item.get("relationships", {}).get("source", {}).get("data", {}).get("id", "unknown")
    dev = directory.get(device_id)
    return {
        "id": item["id"],
        "device_id": dev.id,
        "device_name": dev.name,
        "room": dev.room,
        "indoor": dev.indoor,
        "event_type": event_type,
        "sub_type": sub_type,
        "start_ms": int(attrs["start"]),
        "end_ms": int(attrs.get("end") or attrs["start"]),
        "source": source,
        "raw": item,
    }


def normalize_webhook(payload: dict[str, Any], directory: DeviceDirectory, source: str = "ring_webhook") -> dict[str, Any] | None:
    """Ring webhook v1.1 payload -> CareCircle event row (None for non-activity webhooks)."""
    data = payload.get("data", {})
    event_type = WEBHOOK_TYPE_MAP.get(data.get("type", ""))
    if event_type is None:
        return None
    attrs = data.get("attributes", {})
    dev = directory.get(attrs.get("source", "unknown"))
    ts = int(attrs["timestamp"])
    return {
        "id": data.get("id") or f"{dev.id}_{data.get('type')}_{ts}",
        "device_id": dev.id,
        "device_name": dev.name,
        "room": dev.room,
        "indoor": dev.indoor,
        "event_type": event_type,
        "sub_type": attrs.get("sub_type"),
        "start_ms": ts,
        "end_ms": ts,
        "source": source,
        "raw": payload,
    }


def sign_webhook(secret: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_webhook_signature(secret: str, raw_body: bytes, received: str | None) -> bool:
    """Exactly the documented scheme: hex HMAC-SHA256 over the raw body, 'sha256=' prefix."""
    if not secret or not received:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.removeprefix("sha256="))


class LiveRingClient:
    def __init__(self, token: str, base_url: str = RING_API, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"},
                                  timeout=15, transport=transport)

    def devices(self) -> list[dict[str, Any]]:
        r = self._http.get("/v1/devices")
        r.raise_for_status()
        return r.json().get("data", [])

    def history(self, device_id: str, event_types: str = "motion,ding", max_pages: int = 3) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = f"/v1/history/devices/{device_id}/events"
        params: dict[str, str] | None = {"event_types": event_types}
        for _ in range(max_pages):
            if not url:
                break
            r = self._http.get(url, params=params)
            r.raise_for_status()
            body = r.json()
            items.extend(body.get("data", []))
            nxt = body.get("links", {}).get("next")
            if not body.get("data") or not nxt:
                break
            url, params = nxt, None
        return items

    def sync(self, directory: DeviceDirectory) -> list[dict[str, Any]]:
        out = []
        for d in self.devices():
            directory.learn(d["id"], d.get("attributes", {}).get("name", d["id"]))
            for item in self.history(d["id"]):
                out.append(normalize_history_event(item, directory, "ring_api"))
        return out


class SampleRingSource:
    """Materializes the authored fixture onto *today* in the household time zone."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.doc = json.loads(self.path.read_text(encoding="utf-8"))

    def history_items(self, day_at) -> list[dict[str, Any]]:
        """``day_at(hhmm) -> aware datetime``; returns items in Ring's history-events shape."""
        items = []
        for i, e in enumerate(self.doc["events"]):
            start: datetime = day_at(e["at"])
            start_ms = int(start.timestamp() * 1000)
            raw_type = e["event_type"] + (f".{e['sub_type']}" if e.get("sub_type") else "")
            items.append({
                "type": "history-events",
                "id": f"sample-{start.date().isoformat()}-{i:02d}",
                "attributes": {"event_type": raw_type, "is_third_party_reviewed": False,
                               "start": start_ms, "end": start_ms + 1000 * int(e.get("seconds", 10))},
                "relationships": {"source": {"data": {"type": "devices", "id": e["device"]}}},
            })
        return items

    def events(self, day_at, directory: DeviceDirectory) -> list[dict[str, Any]]:
        return [normalize_history_event(i, directory, "sample") for i in self.history_items(day_at)]

    def medication_log(self) -> list[dict[str, Any]]:
        return list(self.doc.get("medication_log", []))
