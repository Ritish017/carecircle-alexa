"""Groq chat-completions client (OpenAI-compatible) used as the simulator's planner. Optional."""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


class GroqLLM:
    def __init__(self, api_key: str, model: str | None = None, base_url: str = "https://api.groq.com/openai/v1",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.model = model or os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
        self._http = httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"},
                                       timeout=45, transport=transport)

    @classmethod
    def from_env(cls) -> "GroqLLM | None":
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key or os.getenv("LLM_PROVIDER", "groq") == "off":
            return None
        return cls(key)

    @property
    def label(self) -> str:
        return f"Groq {self.model}"

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """One completion. On a 429 (free-tier tokens/minute) waits briefly, then tries the fallback model."""
        models = [self.model] + [m for m in (os.getenv("LLM_FALLBACK_MODEL", "openai/gpt-oss-20b"),) if m != self.model]
        last: httpx.Response | None = None
        for attempt, model in enumerate(models):
            body: dict[str, Any] = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                                    "temperature": 0.2, "max_tokens": 700}
            if "gpt-oss" in model:
                body["reasoning_effort"] = "low"
            r = await self._http.post("/chat/completions", json=body)
            if r.status_code == 429 and attempt + 1 < len(models):
                last = r
                await asyncio.sleep(min(float(r.headers.get("retry-after", "1") or 1), 3))
                continue
            r.raise_for_status()
            self.last_model = model
            return r.json()["choices"][0]["message"]
        assert last is not None
        last.raise_for_status()
        return {}
