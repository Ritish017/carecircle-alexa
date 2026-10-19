# Product feedback, feature requests and friction log

Written from the actual CareCircle build (October 15–16, 2026). Every entry below happened during this build.

## 1. Product feedback (per tool)

### MCP Python SDK (`mcp` 2.3.0), the required Alexa+ runtime technology
- **Used for:** the CareCircle server (`MCPServer`, Streamable HTTP app, structured output, `ctx.elicit`, `Apps` extension, custom routes) and the simulator's client (`Client`, `streamable_http_client`, elicitation callback, message handler).
- **Worked well:** Pydantic return types become `outputSchema` automatically. `ctx.elicit(message, Model)` validates answers. The in-process `Client(server)` makes real protocol tests fast (25 tests run in about 1.5 s). Built-in MCP Apps support (`mcp.server.apps.Apps`) checks that a `ui://` resource exists for every bound tool.
- **Needs work:** see friction entries F1–F4. The 2025-11-25 vs 2026-07-28 split is the biggest source of confusion: the same `Client` behaves differently depending on `mode`.
- **Onboarding:** about 1 hour from install to a working server with elicitation and subscriptions over HTTP. Most of that was reading SDK source, because v2 docs and examples still mix v1 (`FastMCP`) and v2 names.
- **Would build again:** Yes. The protocol features (elicitation, structured output, Apps, subscriptions) are exactly what a voice assistant add-on needs.

### Alexa+ (add-on developer tools)
- **Used for:** the target platform. Not directly usable: the Category SDK, MCP Toolkit, CLI and Web Simulator are partner-only (hackathon FAQ), so we built a simulated Alexa+ front end that is a real MCP client.
- **Worked well:** basing Alexa+ add-ons on open MCP meant the whole server could be built and tested without Amazon-specific tooling.
- **Needs work:** individual setup-guide pages don't say the tools are gated (the FAQ acknowledges this caused confusion). There is no public description of how Alexa+ handles elicitation (on-screen form vs. spoken confirmation) or `notifications/resources/updated` (proactive speech?). Those decide how a consent-first add-on should be designed.
- **Would build again:** Yes, once there is a public simulator or test console.

### Ring Partner API / Ring Developer Playground
- **Used for:** `LiveRingClient` (`GET /v1/devices`, `GET /v1/history/devices/{id}/events` with `event_types` filters and `links.next` pagination) and the `/ring/webhook` receiver (webhook v1.1, HMAC-SHA256 `X-Signature`).
- **Worked well:** the API documentation is unusually complete: real JSON:API examples, a warning to verify HMAC over raw bytes rather than re-serialized JSON, composite filters like `motion.human`. We built and unit-tested the client from the docs alone.
- **Needs work:** see F5. The Playground needs a full Amazon Developer registration (legal name, address, customer-support email, phone, Developer Services Agreement, later identity verification) before you can generate a 30-minute token. That is a lot for "try the API". Distribution is US-only (Ring Appstore), which limits impact for builders outside the US.
- **Would build again:** Yes, for caretaking use cases. Event history + webhooks fit "is everything normal at home?" well.

### Groq (LLM planner, optional)
- **Used for:** tool-calling planner in the simulator (`openai/gpt-oss-120b`, fallback `openai/gpt-oss-20b`).
- **Worked well:** OpenAI-compatible tool calling with MCP tool schemas passed straight through. Fast enough for voice turns.
- **Needs work:** the free tier's 8,000 tokens per minute is easy to hit with tool schemas plus results. We added result compaction and a 429 fallback model.

## 2. Feature requests

| Request | Why it matters for CareCircle | Priority |
|---|---|---|
| Public Alexa+ add-on simulator (even text-only) that accepts a self-hosted MCP URL | Lets builders test real Alexa+ handling of elicitation, MCP Apps and notifications instead of simulating them | Critical |
| Documented Alexa+ behaviour for `elicitation/create` (voice yes/no? Echo Show form?) and for `notifications/resources/updated` (proactive announcement rules) | Consent-first and proactive add-ons depend on it | Critical |
| Ring Playground access without full developer registration (or a sandbox token generator for registered Devpost participants) | Fastest path to real Ring data for prototypes | Important |
| Ring Playground: simulated *history* and webhook events (not only live-view streams) for package/vehicle/motion | Caretaking apps consume event history and webhooks, not video | Important |
| MCP Python SDK: one documented switch for "speak 2025-11-25 over the handshake" vs "2026-07-28", with a migration table for `resources/subscribe` → `subscriptions/listen` and elicitation → `InputRequiredResult` | Avoids silent behaviour changes | Nice-to-have |

## 3. Friction log

**F1. FastMCP import fails on SDK v2.** *Task:* start the server from v1-era examples. *Steps:* `from mcp.server.fastmcp import FastMCP`. *Expected:* import works. *Actual:* `ModuleNotFoundError` whose message points to `MCPServer` (good message, but v1 examples are still everywhere). *Severity:* low. *Workaround:* `from mcp.server.mcpserver import MCPServer`. *Suggestion:* update the getting-started pages and examples linked from the hackathon rules to v2 names.

**F2. Elicitation silently impossible on in-process "auto" clients.** *Task:* test consent with `Client(server, elicitation_callback=...)`. *Expected:* the callback fires. *Actual:* the tool got `NoBackChannelError`, because the default `mode="auto"` dispatches in-process at 2026-07-28 with no server-to-client channel. Our first "decline" tests passed for the wrong reason. *Severity:* high (false-positive safety tests). *Workaround:* `mode="legacy"` in tests, and `mcp-consent-gate` falls back to a confirmation code when this error occurs. *Suggestion:* raise or warn at connect time when a client registers an `elicitation_callback` it can never receive, or document the interaction prominently.

**F3. `resources/subscribe` not wired by `MCPServer`.** *Task:* let the Alexa+ client subscribe to `carecircle://alerts`. *Expected:* `MCPServer` handles `resources/subscribe` for handshake-era clients. *Actual:* no handler is registered (only `subscriptions/listen`), so `capabilities.resources.subscribe` is false at 2025-11-25. The client also emits a deprecation warning. *Severity:* medium. *Workaround:* register handlers on the low-level server (`add_request_handler("resources/subscribe", …)`) and call `session.send_resource_updated(uri)`, while also publishing to the SDK's subscription bus for modern clients. *Suggestion:* a built-in legacy subscribe handler backed by the same bus.

**F4. MCP Apps registration order.** *Task:* bind `check_in` to `ui://carecircle/check-in.html`. *Actual:* `ValueError … no such resource is registered` because the HTML resource was added after `MCPServer(extensions=[apps])`. *Severity:* low. *Workaround:* call `apps.add_html_resource` before constructing the server. *Suggestion:* allow late registration or say "register before constructing MCPServer" in the error.

**F5. Ring Playground behind full developer registration.** *Task:* get a Playground token from India to replace SAMPLE events with real ones. *Steps:* sign in at developer.amazon.com/ring/console/playground with an Amazon account (2-step verification passed). *Expected:* the Playground. *Actual:* redirect to "Amazon Developer Registration" (legal name, address, customer-support email, phone, agree to the Developer Services Agreement; identity verification with government ID later). India is selectable, so it is not a country block. *Severity:* medium (blocks real Ring data for a 1-week hackathon). *Workaround:* `SampleRingSource` replays an authored fixture in the documented shape, clearly labelled SAMPLE. The live client is unit-tested against mocks. *Suggestion:* a lightweight "Playground-only" sign-up, or pre-provisioned sandbox tokens for hackathon participants.

**F6. Hackathon resources link to version-less MCP pages.** *Task:* confirm the minimum spec. *Actual:* rules say "2025-11-25 minimum", while modelcontextprotocol.io now defaults to 2026-07-28, where `resources/subscribe` and server-initiated elicitation work differently. *Severity:* low. *Workaround:* we negotiate 2025-11-25 over the `initialize` handshake and also publish to `subscriptions/listen`. *Suggestion:* state which spec version judges will test against and whether 2026-07-28 behaviour is expected.
