"""Deterministic wellbeing rules. The LLM never decides whether Dad is okay; these rules do."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .clock import Clock
from .models import ActivityItem, CheckInReport, DoseStatus, Evidence, SuggestedAction
from .ring import SOURCE_LABELS
from .store import Store

EVENT_WORDS = {"motion": "movement", "ding": "doorbell pressed", "on_demand": "live view opened"}


def fmt(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").lower()


def _ms_to_local(ms: int, clock: Clock) -> datetime:
    return datetime.fromtimestamp(ms / 1000, clock.tz)


def day_events(store: Store, clock: Clock) -> list[dict[str, Any]]:
    now = clock.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return store.events_between(int(start.timestamp() * 1000), int(now.timestamp() * 1000))


def dose_statuses(plan: dict[str, Any], store: Store, clock: Clock) -> list[DoseStatus]:
    now = clock.now()
    logged = store.doses_on(now.date().isoformat())
    out = []
    for med in plan["medications"]:
        for t in med["times"]:
            due = clock.at(t)
            hit = next((d for d in logged if d["medication"] == med["id"] and d["dose_time"] == t), None)
            if hit:
                try:
                    taken = fmt(clock.at(hit["taken_at"]))
                except ValueError:
                    taken = hit["taken_at"]
                status = "taken"
            elif now > due + timedelta(minutes=med.get("grace_minutes", 60)):
                status, taken = "overdue", None
            elif now >= due - timedelta(minutes=30):
                status, taken = "due_soon", None
            else:
                status, taken = "upcoming", None
            out.append(DoseStatus(medication_id=med["id"], medication=med["name"], purpose=med["purpose"],
                                  due=fmt(due), status=status, taken_at=taken))
    return out


def timeline(store: Store, clock: Clock, hours: int = 24) -> list[ActivityItem]:
    now = clock.now()
    start = now - timedelta(hours=hours)
    rows = store.events_between(int(start.timestamp() * 1000), int(now.timestamp() * 1000))
    return [ActivityItem(time=fmt(_ms_to_local(r["start_ms"], clock)), room=r["room"], device=r["device_name"],
                         event=EVENT_WORDS.get(r["event_type"], r["event_type"]) +
                         (f" ({r['sub_type']})" if r["sub_type"] else ""),
                         source=SOURCE_LABELS.get(r["source"], r["source"])) for r in rows]


def check_in(plan: dict[str, Any], store: Store, clock: Clock) -> CheckInReport:
    now = clock.now()
    name = plan["household"]["display_name"]
    routine = plan["routine"]
    events = day_events(store, clock)
    activity = [e for e in events if e["event_type"] in ("motion", "ding")]
    indoor = [e for e in activity if e["indoor"] and e["event_type"] == "motion"]
    doses = dose_statuses(plan, store, clock)
    sources = sorted({SOURCE_LABELS.get(e["source"], e["source"]) for e in events})

    evidence: list[Evidence] = []
    # Keep the answer short: first activity, front-door events, last activity.
    picked: list[dict[str, Any]] = []
    if activity:
        picked.append(activity[0])
        picked += [e for e in activity if not e["indoor"] and e["event_type"] == "motion"][:1]
        picked += [e for e in activity if e["event_type"] == "ding"][:1]
        picked.append(activity[-1])
    seen = set()
    for e in sorted(picked, key=lambda x: x["start_ms"]):
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        evidence.append(Evidence(time=fmt(_ms_to_local(e["start_ms"], clock)),
                                 what=f"{EVENT_WORDS.get(e['event_type'], e['event_type'])} at {e['device_name']}",
                                 source=SOURCE_LABELS.get(e["source"], e["source"])))
    for d in doses:
        if d.status == "taken":
            evidence.append(Evidence(time=d.taken_at or d.due, what=f"{d.medication} logged as taken",
                                     source="CareCircle medication log"))

    concerns: list[str] = []
    actions: list[SuggestedAction] = []
    status = "ok"
    first = _ms_to_local(activity[0]["start_ms"], clock) if activity else None
    last_ev = (indoor or activity)[-1] if (indoor or activity) else None
    last = _ms_to_local(last_ev["start_ms"], clock) if last_ev else None
    quiet = int((now - last).total_seconds() // 60) if last else None

    active_from, active_until = clock.at(routine["active_from"]), clock.at(routine["active_until"])
    in_active_hours = active_from <= now <= active_until

    overdue = [d for d in doses if d.status == "overdue"]
    for d in overdue:
        concerns.append(f"{d.due} {d.medication} ({d.purpose}) isn't logged yet")
        actions.append(SuggestedAction(kind="remind_parent", detail=f"Remind {name} to take {d.medication}"))
    if overdue:
        status = "attention"

    if not activity and now > clock.at(routine["expected_up_by"]):
        concerns.append(f"No activity seen today and it is past {fmt(clock.at(routine['expected_up_by']))}")
        status = "concern"
    elif quiet is not None and in_active_hours and quiet >= routine["quiet_alert_minutes"]:
        concerns.append(f"No movement for {quiet // 60} h {quiet % 60} min (since {fmt(last)} in the {last_ev['room']})")
        status = "concern"

    if status != "ok":
        others = [c["name"] for c in plan["caregivers"] if c["channel"] != "alexa"]
        if others:
            actions.append(SuggestedAction(kind="notify_caregiver", detail=f"Let {', '.join(others)} know"))
    if not actions:
        actions.append(SuggestedAction(kind="none", detail="Nothing needed right now"))

    # Spoken headline: two sentences max.
    if not activity:
        s1 = f"I haven't seen any movement at {name}'s today."
    else:
        door = next((e for e in activity if not e["indoor"]), None)
        bits = []
        if door:
            bits.append(f"the front door at {fmt(_ms_to_local(door['start_ms'], clock))}")
        bits.append(f"last movement in the {last_ev['room']} at {fmt(last)}")
        s1 = f"{name}'s been up since {fmt(first)}: " + ", ".join(bits) + "."
    if status == "ok":
        s2 = "All medications so far are logged."
    else:
        parts = []
        if overdue:
            meds = " and ".join(f"{d.due} {d.medication.split()[0]} ({d.purpose})" for d in overdue)
            parts.append(f"His {meds} {'is' if len(overdue) == 1 else 'are'}n't logged yet.")
        parts += [c[0].upper() + c[1:] + "." for c in concerns if not c.endswith("isn't logged yet")]
        s2 = " ".join(parts[:2])
    if any(e["source"] == "sample" for e in events):
        sources_note = "sample"
    else:
        sources_note = "live"

    return CheckInReport(
        person=name, as_of=fmt(now), demo_clock=clock.is_demo, status=status,
        headline=f"{s1} {s2}", first_activity=fmt(first) if first else None,
        last_activity=fmt(last) if last else None, last_activity_room=last_ev["room"] if last_ev else None,
        minutes_since_last_activity=quiet, evidence=evidence, doses=doses, concerns=concerns,
        suggested_actions=actions, data_sources=sources + (["(contains SAMPLE data)"] if sources_note == "sample" else []),
    )
