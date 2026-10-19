"""Structured tool outputs (exposed to clients as MCP outputSchema)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    time: str = Field(description="Local time, e.g. '7:42 am'")
    what: str
    source: str = Field(description="Where this fact came from (Ring API, Ring webhook, SAMPLE fixture, medication log)")


class DoseStatus(BaseModel):
    medication_id: str
    medication: str
    purpose: str
    due: str
    status: Literal["taken", "overdue", "due_soon", "upcoming"]
    taken_at: str | None = None


class SuggestedAction(BaseModel):
    kind: Literal["remind_parent", "notify_caregiver", "log_medication", "none"]
    detail: str


class CheckInReport(BaseModel):
    person: str
    as_of: str
    demo_clock: bool = Field(description="True when the household clock is pinned for a reproducible demo")
    status: Literal["ok", "attention", "concern"]
    headline: str = Field(description="One or two spoken sentences")
    first_activity: str | None
    last_activity: str | None
    last_activity_room: str | None
    minutes_since_last_activity: int | None
    evidence: list[Evidence]
    doses: list[DoseStatus]
    concerns: list[str]
    suggested_actions: list[SuggestedAction]
    data_sources: list[str]


class ActivityItem(BaseModel):
    time: str
    room: str
    device: str
    event: str
    source: str


class ActivityTimeline(BaseModel):
    person: str
    since: str
    until: str
    items: list[ActivityItem]
    data_sources: list[str]


class DoseLogResult(BaseModel):
    logged: bool
    medication: str
    dose_time: str
    taken_at: str
    message: str


class FollowUpResult(BaseModel):
    status: Literal["done", "partially_done", "not_approved", "needs_confirmation"]
    reminder: str | None = None
    notifications: list[str] = Field(default_factory=list)
    consent_method: str
    consent_digest: str
    confirmation_code: str | None = None
    message: str


class SyncResult(BaseModel):
    source: str
    fetched: int
    new: int
    message: str
