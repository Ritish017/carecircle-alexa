### Inspiration
Many of us have a parent living alone in another city. The daily check-in is a phone call that either interrupts them or doesn't get answered, and the questions that matter ("Did he get up? Did he take his blood-pressure tablet?") already have answers in the house: the doorbell saw him go out, the kitchen camera saw him make breakfast, the pill log knows what was taken. Nobody joins those facts up. And no family wants an assistant that messages relatives on its own.

### What it does
CareCircle is a self-hosted **MCP server for Alexa+** (spec 2025-11-25, Streamable HTTP):
- **"Alexa, is Dad okay today?"** → `check_in` joins today's **Ring** door, doorbell and motion events with the **medication log** and returns a status (ok / attention / concern), a two-sentence spoken answer ("Dad's been up since 6:58 am: the front door at 7:42 am, last movement in the living room at 1:55 pm. His 8:00 am Amlodipine isn't logged yet."), and evidence. The evidence renders as an **MCP App** card (`ui://carecircle/check-in.html`).
- **"Yes, remind him and tell my sister."** → `follow_up` never acts on its own. It sends an **MCP elicitation** form showing the exact Echo reminder and the exact message to Priya, each as a separate checkbox. Only the approved parts run. Each approval is bound to a SHA-256 digest of the action and written to an audit log.
- **"He just took his BP pill."** → `log_medication` matches "blood pressure pill" to Amlodipine and logs the due dose.
- **Proactive:** when the home goes quiet for two hours during Dad's active day, CareCircle raises an alert. The Alexa+ client is subscribed to `carecircle://alerts`, gets `notifications/resources/updated`, and speaks up first: "No movement since 1:55 pm. Should I let Priya know?" It still asks before sending anything.

### How we built it
- **Server:** Python + the official **MCP Python SDK (2.3)** `MCPServer`. 5 tools with Pydantic **output schemas** and tool annotations, 4 resources, a prompt, the **MCP Apps** extension, `resources/subscribe` for handshake-era clients plus the SDK's `subscriptions/listen` bus for 2026-07-28 clients, an optional bearer token, and SQLite storage.
- **Rules, not vibes:** a deterministic engine decides status (an overdue dose past its grace window = attention; 120 quiet minutes in active hours, or no activity by 8:30 am = concern). The LLM only plans tool calls and phrases the answer.
- **Ring:** a Partner API client (`GET /v1/devices`, `GET /v1/history/devices/{id}/events` with `event_types` filters and `links.next` pagination) and a `/ring/webhook` receiver that verifies Ring's **HMAC-SHA256 `X-Signature`** over the raw body (webhook v1.1).
- **Consent:** our new open-source package **`mcp-consent-gate`**: elicitation-based approval bound to an action digest. It fails closed, supports per-part choices, keeps an audit log, and falls back to a one-time confirmation code for clients without elicitation.
- **Alexa+ experience:** Alexa+ add-on tooling is partner-only, so we built a **simulated Alexa+ experience** as the FAQ allows: an Echo Show-style web page whose backend is a **real MCP client** (official SDK). It sends initialize, tools/list, tools/call and resources/read, answers elicitation requests by showing the consent sheet, renders MCP Apps in a sandboxed iframe (ext-apps postMessage protocol), and speaks with the Web Speech API. Groq `openai/gpt-oss-120b` plans tool calls, with a deterministic rule planner if no key is set.
- **Tests:** 37 pytest tests (25 app + 12 package), including an end-to-end test that runs both servers over HTTP, answers the elicitation, logs a dose, advances the clock and checks that the proactive alert arrives through the subscription.

### Challenges we ran into
- **No Alexa+ console access**, so we built a faithful MCP client and made every protocol step visible in an on-screen MCP trace.
- **Ring Playground access from India** needs a full Amazon Developer registration. Rather than fake it, the demo replays a **clearly labelled SAMPLE fixture** in the documented API shape, and the live client and webhook are tested against mocks built from the docs.
- **MCP SDK v2 changes:** FastMCP was renamed, the 2025-11-25 vs 2026-07-28 modes differ (in-process "auto" clients have no elicitation back channel, which made our first "decline" tests pass for the wrong reason), and `resources/subscribe` needed manual wiring. All of it is written up in the friction log.

### Accomplishments that we're proud of
- Consent that is **tied to content**: approving "remind Dad" can't be replayed to "message Mallory".
- A check-in that reads like a person but is **fully deterministic** underneath.
- A reusable open-source package that any MCP server can adopt in a few lines.

### What we learned
MCP's newer features (elicitation, structured output, Apps, subscriptions) are what turn "a wrapper around an API" into a voice experience a family could trust. And for caretaking, the hard part is not detecting events. It is deciding when *not* to act.

### What's next for CareCircle
Ring OAuth account linking and webhooks on a real account; OAuth 2.1 on `/mcp`; registration in the Alexa+ add-on tooling when it opens; per-family routines learned from a week of history; a weekly summary for siblings; `mcp-consent-gate` support for the 2026-07-28 `InputRequiredResult` flow.

### Honesty notes (also in the README)
Simulated Alexa+ front end (real MCP traffic). Ring events in the demo are an authored SAMPLE fixture, labelled everywhere. The Echo reminder is shown on the page, not played on a real Echo. Messages to caregivers go to a local outbox unless `NTFY_TOPIC` is set. People and medications are fictional. CareCircle is not a medical device.