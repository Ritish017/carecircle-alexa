"""The simulated Alexa+ agent.

It is a genuine MCP client: it connects to the CareCircle server over Streamable HTTP with the official Python SDK
(initialize at protocol 2025-11-25), lists tools, calls them, answers elicitation requests by asking the human in
the browser, subscribes to carecircle://alerts and reacts to notifications/resources/updated.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import mcp.types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .llm import GroqLLM

log = logging.getLogger("alexa_sim")
Emit = Callable[[dict[str, Any]], Awaitable[None]]
ALERTS_URI = "carecircle://alerts"
CARD_URI = "ui://carecircle/check-in.html"

PERSONA = """You are Alexa+, speaking on an Echo Show for an adult son who looks after his father ("Dad").
Speak naturally and briefly: at most two short sentences, then one question if a follow-up makes sense.
Use the CareCircle tools for every fact; never invent times or doses. Mention times like '7:42 am'.
Say what the sensors saw ('no movement detected since 1:55 pm'); never guess where Dad is or what he is doing.
If check_in shows anything needing attention, offer to remind Dad and to let his sister Priya know; do not call
follow_up until the user agrees (follow_up itself will then show the user a consent form). When calling follow_up,
leave 'message' empty unless the user dictated one: CareCircle writes the message to Priya itself.
For a proactive alert, if the user agrees, call follow_up with remind_parent=false and notify=["priya"].
If the data is SAMPLE data, you don't need to say so out loud; the screen labels it."""


def compact(name: str, result: dict[str, Any]) -> str:
    """Keep tool results small: the free LLM tier allows ~8k tokens/minute."""
    if name == "check_in":
        keep = {k: result.get(k) for k in ("status", "headline", "concerns", "as_of", "suggested_actions")}
        keep["doses"] = [f"{d['due']} {d['medication']}: {d['status']}" for d in result.get("doses", [])]
        return json.dumps(keep, default=str)
    if name == "get_activity":
        return json.dumps({"items": [f"{i['time']} {i['event']} @ {i['room']}" for i in result.get("items", [])][-15:]})
    return json.dumps(result, default=str)[:1500]


def tool_to_openai(t: types.Tool) -> dict[str, Any]:
    params = dict(t.input_schema or {"type": "object", "properties": {}})
    params.setdefault("properties", {})
    return {"type": "function", "function": {"name": t.name, "description": (t.description or "")[:1000],
                                             "parameters": params}}


class AlexaAgent:
    def __init__(self, mcp_url: str, emit: Emit, *, token: str | None = None, llm: GroqLLM | None = None) -> None:
        self.mcp_url = mcp_url
        self.emit = emit
        self.token = token
        self.llm = llm
        self.client: Client | None = None
        self.tools: list[types.Tool] = []
        self.instructions = ""
        self.history: list[dict[str, Any]] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._seen_alerts: set[str] = set()
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._turn_lock = asyncio.Lock()
        self.protocol_version: str | None = None
        self._cards: list[dict[str, Any]] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._ready.wait(), timeout=20)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)

    async def _trace_request(self, request: httpx2.Request) -> None:
        if request.method != "POST":
            return
        with contextlib.suppress(Exception):
            body = json.loads(request.content or b"{}")
            if "method" in body:
                detail = body.get("params", {}).get("name") or body.get("params", {}).get("uri") or ""
                await self.emit({"type": "rpc", "dir": "out", "method": body["method"], "detail": detail})
            elif "result" in body or "error" in body:
                await self.emit({"type": "rpc", "dir": "out", "method": "(response to server request)", "detail": ""})

    async def _run(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30, read=300),
                                  event_hooks={"request": [self._trace_request]})
        transport = streamable_http_client(self.mcp_url, http_client=http)
        async with http, Client(transport, mode="legacy", elicitation_callback=self._on_elicit,
                                message_handler=self._on_message,
                                client_info=types.Implementation(name="alexa-plus-simulator", version="0.1.0")) as c:
            self.client = c
            init = c.session.initialize_result
            self.protocol_version = init.protocol_version if init else None
            self.instructions = (init.instructions or "") if init else ""
            self.tools = (await c.list_tools()).tools
            await c.subscribe_resource(ALERTS_URI)
            for a in json.loads(await self._read_text(ALERTS_URI) or "[]"):
                self._seen_alerts.add(a["id"])
            self._ready.set()
            await self.emit({"type": "connected", "protocol": self.protocol_version,
                             "tools": [t.name for t in self.tools], "planner": self.planner_label})
            await self._stop.wait()

    @property
    def planner_label(self) -> str:
        return self.llm.label if self.llm else "offline rule planner (no GROQ_API_KEY)"

    async def _read_text(self, uri: str) -> str:
        assert self.client is not None
        res = await self.client.read_resource(uri)
        return "".join(getattr(c, "text", "") for c in res.contents)

    async def card_html(self) -> str:
        return await self._read_text(CARD_URI)

    # ----------------------------------------------------------- elicitation
    async def _on_elicit(self, context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        eid = uuid.uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[eid] = fut
        schema = getattr(params, "requested_schema", None) or {}
        await self.emit({"type": "elicit", "id": eid, "message": params.message, "schema": schema})
        try:
            answer = await asyncio.wait_for(fut, timeout=170)
        except asyncio.TimeoutError:
            answer = {"action": "cancel"}
        finally:
            self._pending.pop(eid, None)
        await self.emit({"type": "elicit_done", "id": eid, "action": answer.get("action")})
        return types.ElicitResult(action=answer.get("action", "cancel"), content=answer.get("content"))

    def answer_elicitation(self, eid: str, action: str, content: dict[str, Any] | None) -> bool:
        fut = self._pending.get(eid)
        if fut is None or fut.done():
            return False
        fut.set_result({"action": action, "content": content if action == "accept" else None})
        return True

    # ------------------------------------------------------- notifications
    async def _on_message(self, msg: Any) -> None:
        root = getattr(msg, "root", msg)
        if isinstance(root, types.ResourceUpdatedNotification):
            await self.emit({"type": "rpc", "dir": "in", "method": "notifications/resources/updated",
                             "detail": str(root.params.uri)})
            if str(root.params.uri) == ALERTS_URI:
                asyncio.create_task(self._handle_alerts())

    async def _handle_alerts(self) -> None:
        alerts = json.loads(await self._read_text(ALERTS_URI) or "[]")
        fresh = [a for a in alerts if a["id"] not in self._seen_alerts]
        for a in fresh:
            self._seen_alerts.add(a["id"])
            await self.emit({"type": "alert", "text": a["text"]})
            await self.turn(f"[Proactive alert from CareCircle's alerts subscription: {a['text']}] "
                            "Tell me about it in one sentence and ask if you should let Priya know.",
                            proactive=True)

    # ----------------------------------------------------------------- turns
    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.client is not None
        await self.emit({"type": "tool_call", "name": name, "args": args})
        res = await self.client.call_tool(name, args)
        data = res.structured_content
        if data is None:
            text = "".join(getattr(c, "text", "") for c in res.content)
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text}
        await self.emit({"type": "tool_result", "name": name, "result": data, "is_error": bool(res.is_error)})
        if name == "check_in" and not res.is_error:
            self._cards.append({"type": "card", "tool": name, "args": args,
                                "result": res.model_dump(by_alias=True, mode="json", exclude_none=True)})
        return data

    async def turn(self, text: str, proactive: bool = False) -> str:
        async with self._turn_lock:
            if not proactive:
                await self.emit({"type": "user", "text": text})
            try:
                speech = await (self._llm_turn(text) if self.llm else self._rule_turn(text))
            except Exception as exc:  # LLM outage -> rules, never a dead speaker
                log.exception("LLM turn failed; falling back to rules")
                await self.emit({"type": "planner", "label": f"rules (LLM error: {type(exc).__name__})"})
                speech = await self._rule_turn(text)
            await self.emit({"type": "assistant", "text": speech, "proactive": proactive})
            for card in self._cards[-1:]:  # MCP App view under the spoken answer
                await self.emit(card)
            self._cards.clear()
            return speech

    async def _llm_turn(self, text: str) -> str:
        assert self.llm is not None
        tools = [tool_to_openai(t) for t in self.tools]
        messages = [{"role": "system", "content": PERSONA + "\n\nServer instructions:\n" + self.instructions}]
        messages += self.history[-12:]
        messages.append({"role": "user", "content": text})
        for _ in range(6):
            msg = await self.llm.chat(messages, tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                speech = (msg.get("content") or "").strip() or "Sorry, I didn't catch that."
                self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": speech}]
                return speech
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = await self.call_tool(fn["name"], args)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": compact(fn["name"], result)})
        return "I'm having trouble finishing that. Please try again."

    # Deterministic planner: used without an LLM key and as a fallback. Covers the demo intents.
    async def _rule_turn(self, text: str) -> str:
        t = text.lower()
        if t.startswith("[proactive alert"):
            m = re.search(r"subscription: (.*?)\]", text)
            self._last_offer = "notify_only"
            return f"Heads up: {m.group(1) if m else 'something looks unusual at Dad’s'}. Should I let Priya know?"
        if re.search(r"\b(took|taken|has taken|log)\b", t):
            med = "blood pressure" if "pressure" in t or "bp" in t else ("metformin" if "metformin" in t or "sugar" in t else "blood pressure")
            r = await self.call_tool("log_medication", {"medication": med})
            return r.get("message", "Done.")
        if re.search(r"\b(yes|yeah|sure|please do|go ahead|ok|okay)\b", t) and getattr(self, "_last_offer", None):
            offer = self._last_offer
            self._last_offer = None
            args = {"remind_parent": offer != "notify_only", "notify": ["priya"]}
            r = await self.call_tool("follow_up", args)
            if r.get("status") in ("done", "partially_done"):
                said = []
                if r.get("reminder"):
                    said.append("I've reminded Dad on his Echo")
                if r.get("notifications"):
                    said.append("let Priya know")
                return (" and ".join(said) or "Done") + "."
            if r.get("status") == "needs_confirmation":
                return f"Please confirm: say the code {r.get('confirmation_code')}."
            return "Okay, I won't contact anyone."
        if re.search(r"\b(no|don't|do not|nope)\b", t) and getattr(self, "_last_offer", None):
            self._last_offer = None
            return "Okay, I won't contact anyone."
        if re.search(r"\b(remind|tell priya|let priya|notify)\b", t):
            r = await self.call_tool("follow_up", {"remind_parent": "remind" in t, "notify": ["priya"] if "priya" in t else []})
            return r.get("message", "Done.")
        if re.search(r"\b(activity|timeline|what happened)\b", t):
            r = await self.call_tool("get_activity", {"hours": 12})
            items = r.get("items", [])[-3:]
            return "Most recent: " + "; ".join(f"{i['event']} at the {i['room']} at {i['time']}" for i in items) + "."
        r = await self.call_tool("check_in", {})
        if r.get("status") != "ok":
            self._last_offer = "remind_and_notify"
            return r["headline"] + " Want me to remind him and let Priya know?"
        return r["headline"]
