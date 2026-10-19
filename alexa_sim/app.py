"""FastAPI host for the Alexa+ simulator page.

Run (after starting the CareCircle server):  python -m alexa_sim.app   ->  http://127.0.0.1:8090
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .agent import AlexaAgent
from .llm import GroqLLM

STATIC = Path(__file__).parent / "static"
CARECIRCLE_URL = os.getenv("CARECIRCLE_URL", "http://127.0.0.1:8080").rstrip("/")


class Hub:
    """Fan-out of agent events to every open browser tab (Server-Sent Events)."""

    def __init__(self) -> None:
        self.queues: set[asyncio.Queue] = set()
        self.log: list[dict] = []

    async def emit(self, event: dict) -> None:
        self.log.append(event)
        self.log = self.log[-300:]
        for q in list(self.queues):
            q.put_nowait(event)


def create_app(llm: GroqLLM | None | str = "env") -> FastAPI:
    hub = Hub()
    planner = GroqLLM.from_env() if llm == "env" else llm
    agent = AlexaAgent(f"{CARECIRCLE_URL}/mcp", hub.emit, token=os.getenv("CARECIRCLE_TOKEN") or None, llm=planner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await agent.start()
        yield
        await agent.stop()

    app = FastAPI(title="Alexa+ simulator for CareCircle", lifespan=lifespan)
    app.state.agent = agent
    app.state.hub = hub
    http = httpx.AsyncClient(base_url=CARECIRCLE_URL, timeout=20)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    async def info() -> JSONResponse:
        return JSONResponse({"protocol": agent.protocol_version, "tools": [t.name for t in agent.tools],
                             "planner": agent.planner_label, "mcp_url": agent.mcp_url})

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q: asyncio.Queue = asyncio.Queue()
        hub.queues.add(q)

        async def gen():
            try:
                yield f"data: {json.dumps({'type': 'hello', 'planner': agent.planner_label, 'protocol': agent.protocol_version})}\n\n"
                for past in [e for e in hub.log if e.get("type") == "rpc"][-40:]:  # replay MCP trace history
                    yield f"data: {json.dumps(dict(past, replay=True), default=str)}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                hub.queues.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/ask")
    async def ask(request: Request) -> JSONResponse:
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        speech = await agent.turn(text)
        return JSONResponse({"speech": speech})

    @app.post("/api/elicit/{eid}")
    async def elicit(eid: str, request: Request) -> JSONResponse:
        body = await request.json()
        ok = agent.answer_elicitation(eid, body.get("action", "cancel"), body.get("content"))
        return JSONResponse({"ok": ok})

    @app.get("/api/card", response_class=HTMLResponse)
    async def card() -> HTMLResponse:
        return HTMLResponse(await agent.card_html())  # fetched from the MCP server via resources/read

    @app.get("/api/state")
    async def state() -> JSONResponse:
        r = await http.get("/api/state")
        return JSONResponse(r.json())

    @app.post("/api/demo/{action}")
    async def demo(action: str, request: Request) -> JSONResponse:
        if action not in ("reset", "clock", "inject"):
            return JSONResponse({"error": "unknown"}, status_code=404)
        body = await request.body()
        r = await http.post(f"/demo/{action}", content=body or b"{}", headers={"content-type": "application/json"})
        return JSONResponse(r.json(), status_code=r.status_code)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("SIM_PORT", "8090")))


if __name__ == "__main__":
    main()
