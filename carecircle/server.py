"""CareCircle MCP server (MCP spec 2025-11-25 over Streamable HTTP; official Python SDK).

Run:  python -m carecircle.server            ->  http://127.0.0.1:8080/mcp
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

import mcp.types as types
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.subscriptions import ResourceUpdated
from mcp_consent_gate import ConsentGate, PendingConfirmation, Proposal
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import engine
from .clock import Clock, parse_time
from .models import (ActivityTimeline, CheckInReport, DoseLogResult, FollowUpResult, SyncResult)
from .notify import send_caregiver
from .ring import (DeviceDirectory, LiveRingClient, SampleRingSource, normalize_webhook, sign_webhook,
                   verify_webhook_signature)
from .store import Store

log = logging.getLogger("carecircle")
HERE = Path(__file__).parent
ALERTS_URI = "carecircle://alerts"
TODAY_URI = "carecircle://today"
PLAN_URI = "carecircle://care-plan"
CARD_URI = "ui://carecircle/check-in.html"
DEMO_WEBHOOK_SECRET = "carecircle-local-demo-secret"

INSTRUCTIONS = """CareCircle helps an adult child check on an ageing parent who lives alone.
- For any 'is Dad okay / how is Dad' question, call check_in first and answer from its headline and evidence. Never guess.
- Facts come from Ring door/motion events and the medication log. If data_sources mention SAMPLE, the events are demo fixtures.
- Never contact anyone directly. To remind the parent or tell a sibling, call follow_up: it asks the user for consent
  (elicitation) and only acts on what they approve. If it returns needs_confirmation, read the summary to the user and,
  only if they say yes, call follow_up again with the same arguments and the confirmation_code.
- When the user says the parent took a medication, call log_medication.
- Subscribe to carecircle://alerts to hear about unusual quiet periods as they happen."""


class CareCircle:
    """Application state shared by MCP tools and HTTP routes."""

    def __init__(self, plan_path: str | Path | None = None, db_path: str | Path = ":memory:",
                 sample_path: str | Path | None = None, clock: Clock | None = None) -> None:
        self.plan: dict[str, Any] = json.loads(Path(plan_path or HERE / "fixtures/care_plan.json").read_text())
        self.store = Store(db_path)
        self.clock = clock or Clock.from_env(self.plan["household"]["timezone"])
        self.directory = DeviceDirectory(self.plan["ring_devices"])
        self.sample = SampleRingSource(sample_path or HERE / "fixtures/ring_sample_events.json")
        self.gate = ConsentGate(self.store, timeout_seconds=float(os.getenv("CONSENT_TIMEOUT_SECONDS", "180")),
                                clock=lambda: self.clock.now().timestamp())
        self.webhook_secret = os.getenv("RING_WEBHOOK_SECRET", "") or DEMO_WEBHOOK_SECRET
        self.ring_token = os.getenv("RING_ACCESS_TOKEN", "").strip()
        self.subscribers: dict[str, set[Any]] = {}
        self.bus = None  # set by build_server()

    # ------------------------------------------------------------- data in
    def load_sample_day(self) -> int:
        """Reset today's data and replay the SAMPLE fixture (events + medication log)."""
        self.store.reset()
        n = 0
        for e in self.sample.events(self.clock.at, self.directory):
            n += int(self.store.upsert_event(e))
        today = self.clock.today().isoformat()
        for d in self.sample.medication_log():
            self.store.log_dose(d["medication"], today, d["dose_time"], d["taken_at"], d["logged_by"],
                                source="sample")
        return n

    def sync_ring(self) -> SyncResult:
        if self.ring_token:
            events = LiveRingClient(self.ring_token).sync(self.directory)
            new = sum(int(self.store.upsert_event(e)) for e in events)
            return SyncResult(source="Ring Partner API", fetched=len(events), new=new,
                              message=f"Pulled {len(events)} events from api.amazonvision.com ({new} new).")
        events = self.sample.events(self.clock.at, self.directory)
        new = sum(int(self.store.upsert_event(e)) for e in events)
        return SyncResult(source="SAMPLE fixture", fetched=len(events), new=new,
                          message="No RING_ACCESS_TOKEN set, so replayed the authored SAMPLE events "
                                  "(not real Ring data).")

    # ------------------------------------------------------------ data out
    def check_in(self) -> CheckInReport:
        return engine.check_in(self.plan, self.store, self.clock)

    async def publish(self, uri: str) -> None:
        """Tell subscribers a resource changed: legacy resources/subscribe sessions and 2026-07-28 listeners."""
        for session in list(self.subscribers.get(uri, ())):
            try:
                await session.send_resource_updated(uri)
            except Exception:  # session closed
                self.subscribers[uri].discard(session)
        if self.bus is not None:
            await self.bus.publish(ResourceUpdated(uri=uri))

    async def evaluate_alerts(self) -> list[dict[str, Any]]:
        report = self.check_in()
        new: list[dict[str, Any]] = []
        quiet = [c for c in report.concerns if c.startswith(("No movement", "No activity"))]
        day = self.clock.today().isoformat()
        if quiet:
            alert_id = f"{day}-quiet-{report.last_activity or 'none'}"
            if self.store.raise_alert(alert_id, self.clock.iso(), "quiet", "high", quiet[0]):
                new.append({"id": alert_id, "text": quiet[0]})
        else:
            for a in self.store.alerts("open"):
                if a["kind"] == "quiet":
                    self.store.close_alert(a["id"])
        await self.publish(TODAY_URI)
        if new:
            await self.publish(ALERTS_URI)
        return new

    def state(self) -> dict[str, Any]:
        report = self.check_in()
        return {
            "now": engine.fmt(self.clock.now()),
            "demo_clock": self.clock.is_demo,
            "ring_mode": "live" if self.ring_token else "sample",
            "household": self.plan["household"],
            "report": report.model_dump(),
            "timeline": [i.model_dump() for i in engine.timeline(self.store, self.clock, 24)],
            "reminders": [dict(r, time_local=self._local(r["ts"])) for r in self.store.reminders()],
            "notifications": [dict(n, time_local=self._local(n["ts"])) for n in self.store.notifications()],
            "consent": [dict(c, time_local=engine.fmt(datetime.fromtimestamp(c["ts"], self.clock.tz)))
                        for c in self.store.consent_log()],
            "alerts": self.store.alerts(),
        }

    # ----------------------------------------------------------- helpers
    def _local(self, iso: str) -> str:
        try:
            return engine.fmt(datetime.fromisoformat(iso).astimezone(self.clock.tz))
        except ValueError:
            return iso

    def match_medication(self, text: str) -> dict[str, Any] | None:
        text = text.lower().strip()
        for m in self.plan["medications"]:
            keys = [m["id"], m["name"].lower(), m["purpose"].lower()]
            if any(k in text or text in k for k in keys):
                return m
        names = {m["name"].lower(): m for m in self.plan["medications"]}
        hit = difflib.get_close_matches(text, list(names), n=1, cutoff=0.5)
        return names[hit[0]] if hit else None

    def caregiver(self, ref: str) -> dict[str, Any] | None:
        ref = ref.lower().strip()
        return next((c for c in self.plan["caregivers"] if ref in (c["id"], c["name"].lower(), c["relation"])), None)


def build_server(cc: CareCircle) -> MCPServer:
    apps = Apps()
    name = cc.plan["household"]["display_name"]

    @apps.tool(resource_uri=CARD_URI, name="check_in", title=f"Check on {name}",
               description=f"How is {name} doing right now? Combines Ring door/motion activity with the medication "
                           "log into a status (ok / attention / concern), a spoken headline, evidence and suggested "
                           "follow-ups. Read-only.",
               annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def check_in() -> CheckInReport:
        return cc.check_in()

    apps.add_html_resource(CARD_URI, (HERE / "ui/check_in_card.html").read_text(), name="check-in-card",
                           title="Check-in card", description="MCP Apps view that renders a check_in result",
                           prefers_border=True)
    mcp = MCPServer("carecircle", title="CareCircle", version="0.1.0", instructions=INSTRUCTIONS,
                    description="Alexa+ add-on: check on an ageing parent with Ring activity + medication log, "
                                "with consent before contacting anyone.",
                    extensions=[apps])
    cc.bus = mcp._subscriptions  # in-process fan-out for subscriptions/listen (2026-07-28 clients)

    @mcp.tool(title="Recent activity", annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
              description=f"Timeline of Ring door, doorbell and motion events at {name}'s home for the last N hours.")
    def get_activity(hours: int = 12) -> ActivityTimeline:
        hours = max(1, min(hours, 72))
        now = cc.clock.now()
        items = engine.timeline(cc.store, cc.clock, hours)
        return ActivityTimeline(person=name, since=f"{hours} h before {engine.fmt(now)}", until=engine.fmt(now),
                                items=items, data_sources=sorted({i.source for i in items}))

    @mcp.tool(title="Log a medication dose",
              annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                                openWorldHint=False),
              description=f"Record that {name} took a medication. 'medication' can be the name or what it is for "
                          "(e.g. 'blood pressure pill'). dose_time defaults to the most recent unlogged dose.")
    async def log_medication(medication: str, ctx: Context, dose_time: str | None = None,
                             taken_at: str | None = None, logged_by: str = "family member via Alexa") -> DoseLogResult:
        med = cc.match_medication(medication)
        if med is None:
            known = ", ".join(m["name"] for m in cc.plan["medications"])
            return DoseLogResult(logged=False, medication=medication, dose_time="", taken_at="",
                                 message=f"I don't know '{medication}'. Known medications: {known}.")
        doses = [d for d in engine.dose_statuses(cc.plan, cc.store, cc.clock) if d.medication_id == med["id"]]
        if dose_time is not None:
            wanted = parse_time(dose_time)
            if wanted not in med["times"]:
                dose_time = None  # not a scheduled slot: fall back to the most recent unlogged dose
            else:
                dose_time = wanted
        if dose_time is None:
            pending = [d for d in doses if d.status in ("overdue", "due_soon")]
            target = pending[0] if pending else next((d for d in doses if d.status != "taken"), None)
            if target is None:
                return DoseLogResult(logged=False, medication=med["name"], dose_time="", taken_at="",
                                     message=f"All of today's {med['name']} doses are already logged.")
            dose_time = next(t for t in med["times"] if engine.fmt(cc.clock.at(t)) == target.due)
        taken = parse_time(taken_at) or cc.clock.now().strftime("%H:%M")
        cc.store.log_dose(med["id"], cc.clock.today().isoformat(), dose_time, taken, logged_by)
        await cc.evaluate_alerts()
        return DoseLogResult(logged=True, medication=med["name"], dose_time=engine.fmt(cc.clock.at(dose_time)),
                             taken_at=taken, message=f"Logged {med['name']} ({engine.fmt(cc.clock.at(dose_time))} dose) "
                                                     f"as taken at {engine.fmt(cc.clock.at(taken))}.")

    @mcp.tool(title="Follow up (asks first)",
              annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                                openWorldHint=True),
              description=f"Remind {name} on his Echo and/or message family caregivers (e.g. 'priya'). ALWAYS asks the "
                          "user to approve the exact wording first (MCP elicitation); only approved parts run. If the "
                          "client cannot show a form it returns needs_confirmation with a code: ask the user, then call "
                          "again with identical arguments plus confirmation_code.")
    async def follow_up(
        ctx: Context,
        remind_parent: Annotated[bool, Field(description="Play a reminder on the parent's Echo")] = True,
        notify: Annotated[list[str] | None, Field(description="Caregivers to message, e.g. ['priya']")] = None,
        reason: Annotated[str, Field(description="Short reason, used only if there is no open concern")] = "",
        message: Annotated[str | None, Field(description="Text for the caregivers. Leave empty: CareCircle writes "
                                                         "a factual message from the check-in.")] = None,
        confirmation_code: Annotated[str | None, Field(description="Only when a previous call returned "
                                                                   "needs_confirmation and the user said yes")] = None,
    ) -> FollowUpResult:
        report = cc.check_in()
        notify = notify or []
        overdue = [d for d in report.doses if d.status == "overdue"]
        concern_text = "; ".join(report.concerns) or reason or "routine check-in"
        reminder_text = None
        if remind_parent:
            if overdue:
                d = overdue[0]
                reminder_text = (f"Hi {name}, gentle reminder: your {d.due} {d.medication.split()[0]}, "
                                 f"the {d.purpose} tablet, isn't marked as taken yet.")
            else:
                reminder_text = f"Hi {name}, just checking in. Your family is thinking of you."
        recipients = [c for c in (cc.caregiver(r) for r in notify) if c is not None]
        unknown = [r for r in notify if cc.caregiver(r) is None]
        msg = message or (f"CareCircle check-in for {name} at {report.as_of}: {concern_text}."
                          + (" I've asked Alexa to remind him." if reminder_text else ""))
        choices: dict[str, str] = {}
        details: dict[str, Any] = {}
        if reminder_text:
            choices["remind_parent"] = f"Play on {cc.plan['household']['echo_device']}: \"{reminder_text}\""
            details["reminder"] = reminder_text
        for c in recipients:
            choices[f"notify_{c['id']}"] = f"Send {c['name']} ({c['relation']}): \"{msg}\""
            details[f"to_{c['id']}"] = msg
        if not choices:
            return FollowUpResult(status="not_approved", consent_method="none", consent_digest="",
                                  message="Nothing to do" + (f"; unknown caregiver(s): {', '.join(unknown)}" if unknown else "."))
        proposal = Proposal(action="follow_up", summary=f"OK to follow up about {name}? Untick anything you don't want.",
                            details=details, choices=choices)
        decision = await cc.gate.request(ctx, proposal, confirmation_code=confirmation_code)
        if isinstance(decision, PendingConfirmation):
            return FollowUpResult(status="needs_confirmation", consent_method="confirmation_code",
                                  consent_digest=decision.digest, confirmation_code=decision.code,
                                  message=decision.message + " Summary: " + " | ".join(choices.values()))
        if not decision.approved:
            return FollowUpResult(status="not_approved", consent_method=decision.method,
                                  consent_digest=decision.digest, message=f"Nothing was sent ({decision.reason}).")
        ts = cc.clock.iso()
        done_reminder, sent = None, []
        if reminder_text and decision.chose("remind_parent"):
            cc.store.add_reminder(ts, cc.plan["household"]["echo_device"], reminder_text, "announced (simulated Echo)",
                                  decision.digest)
            done_reminder = reminder_text
        for c in recipients:
            if decision.chose(f"notify_{c['id']}"):
                status, detail = send_caregiver(c["name"], c["channel"], msg, title=f"CareCircle: {name}")
                cc.store.add_notification(ts, c["name"], c["channel"], msg, status, detail, decision.digest)
                sent.append(f"{c['name']}: {status} ({detail})")
        await cc.publish(TODAY_URI)
        wanted = len(choices)
        did = int(done_reminder is not None) + len(sent)
        return FollowUpResult(status="done" if did == wanted else "partially_done", reminder=done_reminder,
                              notifications=sent, consent_method=decision.method, consent_digest=decision.digest,
                              message=f"Approved {did} of {wanted} actions." +
                                      (f" Unknown caregiver(s): {', '.join(unknown)}." if unknown else ""))

    @mcp.tool(title="Sync Ring events", annotations=types.ToolAnnotations(readOnlyHint=False, idempotentHint=True,
                                                                         openWorldHint=True),
              description="Pull the latest door/motion events from the Ring Partner API (needs RING_ACCESS_TOKEN); "
                          "without a token it replays the clearly-labelled SAMPLE fixture.")
    async def sync_ring() -> SyncResult:
        result = cc.sync_ring()
        await cc.evaluate_alerts()
        return result

    # ------------------------------------------------------------ resources

    @mcp.resource(PLAN_URI, name="care-plan", title="Care plan", mime_type="application/json",
                  description="Household, caregivers, medication schedule, Ring devices and routine thresholds.")
    def care_plan() -> str:
        return json.dumps(cc.plan, indent=2)

    @mcp.resource(ALERTS_URI, name="alerts", title="Open alerts", mime_type="application/json",
                  description="Unusual-quiet alerts raised by the routine rules. Subscribe to get notified.")
    def alerts() -> str:
        return json.dumps(cc.store.alerts("open"), indent=2)

    @mcp.resource(TODAY_URI, name="today", title="Today at home", mime_type="application/json",
                  description="Today's activity timeline and dose status. Changes whenever events or doses arrive.")
    def today() -> str:
        return json.dumps({"timeline": [i.model_dump() for i in engine.timeline(cc.store, cc.clock, 24)],
                           "doses": [d.model_dump() for d in engine.dose_statuses(cc.plan, cc.store, cc.clock)]},
                          indent=2)

    @mcp.prompt(name="daily_check_in", title="Daily check-in",
                description="Ask the assistant to check on the parent and offer follow-ups.")
    def daily_check_in(person: str = name) -> str:
        return (f"Check on {person} using check_in. Tell me in two sentences how he is, citing times. If anything "
                f"needs attention, offer to remind him and to let my sister know, using follow_up.")

    # legacy (2025-11-25) resources/subscribe + unsubscribe
    async def on_subscribe(ctx: Any, params: types.SubscribeRequestParams) -> types.EmptyResult:
        cc.subscribers.setdefault(str(params.uri), set()).add(ctx.session)
        return types.EmptyResult()

    async def on_unsubscribe(ctx: Any, params: types.UnsubscribeRequestParams) -> types.EmptyResult:
        cc.subscribers.get(str(params.uri), set()).discard(ctx.session)
        return types.EmptyResult()

    mcp._lowlevel_server.add_request_handler("resources/subscribe", types.SubscribeRequestParams, on_subscribe)
    mcp._lowlevel_server.add_request_handler("resources/unsubscribe", types.UnsubscribeRequestParams, on_unsubscribe)

    # ------------------------------------------------------------ HTTP routes
    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "ring_mode": "live" if cc.ring_token else "sample"})

    @mcp.custom_route("/api/state", methods=["GET"])
    async def api_state(request: Request) -> Response:
        return JSONResponse(cc.state())

    @mcp.custom_route("/ring/webhook", methods=["POST"])
    async def ring_webhook(request: Request) -> Response:
        raw = await request.body()
        return await handle_webhook(raw, request.headers.get("X-Signature"))

    async def handle_webhook(raw: bytes, signature: str | None) -> Response:
        if not verify_webhook_signature(cc.webhook_secret, raw, signature):
            return JSONResponse({"error": "bad signature"}, status_code=401)
        payload = json.loads(raw)
        source = "sample" if payload.get("meta", {}).get("sample") else "ring_webhook"
        event = normalize_webhook(payload, cc.directory, source)
        if event is None:
            return JSONResponse({"ok": True, "ignored": payload.get("data", {}).get("type")})
        new = cc.store.upsert_event(event)
        alerts_raised = await cc.evaluate_alerts() if new else []
        return JSONResponse({"ok": True, "stored": new, "alerts": alerts_raised})

    @mcp.custom_route("/demo/inject", methods=["POST"])
    async def demo_inject(request: Request) -> Response:
        """Build a SAMPLE Ring webhook (labelled meta.sample) for a room, sign it, and run it through /ring/webhook."""
        body = await request.json()
        room = body.get("room", "living room")
        dev = next((d for d in cc.directory.all() if d.room == room), cc.directory.all()[0])
        hhmm = body.get("at")
        when = cc.clock.at(hhmm) if hhmm else cc.clock.now()
        ts = int(when.timestamp() * 1000)
        kind = {"motion": "motion_detected", "ding": "button_press"}[body.get("event", "motion")]
        payload = {"meta": {"version": "1.1", "time": cc.clock.iso(), "request_id": f"demo-{ts}",
                            "account_id": "ava1.ring.account.SAMPLE", "sample": True},
                   "data": {"id": f"{dev.id}_{kind}_{ts}", "type": kind,
                            "attributes": {"source": dev.id, "source_type": "devices", "timestamp": ts,
                                           "sub_type": "human" if kind == "motion_detected" else None}}}
        raw = json.dumps(payload).encode()
        return await handle_webhook(raw, sign_webhook(cc.webhook_secret, raw))

    @mcp.custom_route("/demo/clock", methods=["POST"])
    async def demo_clock(request: Request) -> Response:
        body = await request.json()
        if "pin" in body:
            cc.clock.pin(body["pin"])
        if "advance" in body:
            cc.clock.advance(int(body["advance"]))
        alerts_raised = await cc.evaluate_alerts()
        return JSONResponse({"now": engine.fmt(cc.clock.now()), "alerts": alerts_raised})

    @mcp.custom_route("/demo/reset", methods=["POST"])
    async def demo_reset(request: Request) -> Response:
        n = cc.load_sample_day()
        await cc.evaluate_alerts()
        return JSONResponse({"ok": True, "sample_events": n})

    return mcp


def build_app(cc: CareCircle | None = None):
    """Starlette app with /mcp (Streamable HTTP) plus the dashboard/webhook routes."""
    cc = cc or CareCircle(db_path=os.getenv("CARECIRCLE_DB", ":memory:"))
    mcp = build_server(cc)
    app = mcp.streamable_http_app(host=os.getenv("HOST", "127.0.0.1"))
    token = os.getenv("CARECIRCLE_TOKEN", "").strip()
    if token:
        from starlette.middleware.base import BaseHTTPMiddleware

        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path.startswith("/mcp") and request.headers.get("authorization") != f"Bearer {token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                return await call_next(request)

        app.add_middleware(BearerAuth)
    app.state.carecircle = cc
    app.state.mcp = mcp
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    cc = CareCircle(db_path=os.getenv("CARECIRCLE_DB", ":memory:"))
    if os.getenv("CARECIRCLE_SAMPLE_DAY", "1") == "1" and not cc.ring_token:
        cc.load_sample_day()
    app = build_app(cc)
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
