"""Household clock. Real time by default; a pinned 'demo clock' makes the recorded demo reproducible."""
from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


_TIME = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?\s*m?\.?\s*$", re.I)


def parse_time(text: str | None) -> str | None:
    """'8', '8:05', '08:05', '8 am', '3:10 PM', '15:10' -> 'HH:MM' (None if unparseable)."""
    if not text:
        return None
    m = _TIME.match(str(text))
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mins < 60):
        return None
    return f"{h:02d}:{mins:02d}"


class Clock:
    def __init__(self, tz: str, pinned: str | None = None) -> None:
        self.tz = ZoneInfo(tz)
        self._lock = threading.Lock()
        self._pinned: datetime | None = None
        if pinned:
            self.pin(pinned)

    @classmethod
    def from_env(cls, tz: str) -> "Clock":
        value = os.getenv("CARECIRCLE_CLOCK", "real").strip()
        return cls(tz, None if value.lower() == "real" else value)

    @property
    def is_demo(self) -> bool:
        return self._pinned is not None

    def pin(self, hhmm: str) -> datetime:
        """Pin the clock to HH:MM today (household time zone)."""
        h, m = (int(x) for x in (parse_time(hhmm) or "12:00").split(":"))
        today = datetime.now(self.tz).date()
        with self._lock:
            self._pinned = datetime.combine(today, time(h, m), self.tz)
            return self._pinned

    def advance(self, minutes: int) -> datetime:
        with self._lock:
            if self._pinned is None:
                raise RuntimeError("advance() only works on a pinned demo clock")
            self._pinned += timedelta(minutes=minutes)
            return self._pinned

    def release(self) -> None:
        with self._lock:
            self._pinned = None

    def now(self) -> datetime:
        with self._lock:
            return self._pinned if self._pinned is not None else datetime.now(self.tz)

    def iso(self) -> str:
        """RFC 3339 timestamp (explicit strftime: some runtimes patch aware isoformat())."""
        n = self.now()
        off = n.strftime("%z")
        return n.strftime("%Y-%m-%dT%H:%M:%S") + (off[:3] + ":" + off[3:] if off else "")

    def today(self) -> date:
        return self.now().date()

    def at(self, hhmm: str, day: date | None = None) -> datetime:
        parsed = parse_time(hhmm)
        if parsed is None:
            raise ValueError(f"not a time: {hhmm!r}")
        h, m = (int(x) for x in parsed.split(":"))
        return datetime.combine(day or self.today(), time(h, m), self.tz)
