# Architecture & contracts

```
Reviewer's browser (static/)                       Server (app/, Python 3.12, FastAPI)
┌────────────────────────────┐   WebSocket /ws    ┌──────────────────────────────────────────┐
│ mic → AudioWorklet PCM16   │ ─────────────────▶ │ session.py  (one per connection)         │
│ 16 kHz                     │                    │   ├─ live.py  ⇄ Gemini Live API (audio)  │
│ speaker ← PCM16 24 kHz     │ ◀───────────────── │   │     tool calls ↓                      │
│ live view ← JPEG frames    │                    │   ├─ crm/tools.py  (verified actions)    │
│ transcript + action log    │                    │   │     ↓                                 │
└────────────────────────────┘                    │   └─ crm/browser.py Playwright Chromium  │
                                                  │         (own context, CDP screencast) ───┼─▶ demo.us.espocrm.com
                                                  └──────────────────────────────────────────┘
```

## Layout
```
app/
  config.py        env settings (see "Env" below)
  server.py        FastAPI: GET / → static/index.html; /static; WS /ws; GET /healthz; GET /api/metrics
  session.py       orchestrates one reviewer session: client WS ⇄ live ⇄ tools ⇄ browser; emits events
  live.py          Gemini Live wrapper (google-genai SDK, client.aio.live.connect)
  prompts.py       system instruction (built from ui_map.json)
  metrics.py       per-turn/per-tool timings, task outcomes → JSONL (data/metrics.jsonl) + summary
  crm/browser.py   CrmBrowser: launch/pool, new context per session, login, screencast, highlight
  crm/tools.py     TOOL_DECLARATIONS (Gemini function declarations) + async implementations
  crm/ui_map.py    load/query ui_map.json
crawler/crawl.py   crawls EspoCRM → data/ui_map.json (entities, fields, labels, types, enum options, actions)
evals/run_evals.py scripted multi-turn scenarios (text mode) × N runs → evals/REPORT.md + evals/results/*.json
tests/             pytest: tools against live demo (marked `live`), unit tests for pure logic
static/            index.html, app.js, audio-worklet.js, styles.css
docs/              BRIEF.md, ARCHITECTURE.md, REQUIREMENTS.md, TECHNICAL_NOTES.md (→ PDF)
Dockerfile         HF Spaces (Docker SDK), port 7860, playwright chromium installed
```

## WebSocket protocol (`/ws`) — the contract between frontend and backend
Client → server
- **binary**: raw PCM16 little-endian mono **16 kHz** mic audio (~20–100 ms chunks).
- **text JSON**:
  - `{"type":"start"}` begin session (server launches browser context + Gemini session)
  - `{"type":"text","text":"..."}` typed message (fallback when no mic)
  - `{"type":"mic","on":true|false}` mic toggled (server sends audio_stream_end when off)
  - `{"type":"stop"}` end session

Server → client
- **binary**: first byte = kind. `0x01` + PCM16 mono **24 kHz** agent audio. `0x02` + JPEG live-view frame.
- **text JSON** (`type` field):
  - `status` `{state:"starting"|"ready"|"ended"|"busy", message}`
  - `transcript` `{role:"user"|"agent", text, final:bool}` (incremental; final closes the bubble)
  - `interrupted` `{}` → client must immediately flush queued agent audio
  - `turn_complete` `{}`
  - `action` `{id, name, args, status:"running"|"ok"|"error", summary, ms}` (tool timeline)
  - `form` `{entity, fields:{label:value}, url}` current form/record snapshot (optional, nice to show)
  - `metrics` `{first_audio_ms, tool_ms, turn_ms}` per turn
  - `error` `{message}`

## Tool design (crm/tools.py)
High-level, **verified** actions. Every tool returns `{ok, observation, ...}` where observation is what is
**actually on screen after the action** (read back from DOM), so the model never claims success blindly.
Minimum set:
- `get_screen()` → page kind (list/detail/edit/create), entity, record name, visible field values, errors/notifications.
- `open_create_form(entity)` → Contact | Opportunity | Account | Lead.
- `fill_fields(fields: {fieldName: value})` → types like a human (visible), handles selectize enums, phone/email, name parts;
  returns read-back values per field.
- `save()` → clicks Save, waits for detail view or validation error; returns saved values read from the detail view + record URL.
- `cancel()`.
- `find_records(entity, query)` → search via list view; returns top matches (name, id).
- `open_record(entity, id|name)`.
- `edit_record()` (enter edit mode on detail view).
- `set_stage(stage)` convenience for Opportunity (inline/edit), verified.
Every browser action highlights the target element briefly (outline) so viewers can follow.
Field names/labels/enum options come from `data/ui_map.json` (crawler), with a static fallback.

## Conversation policy (prompts.py)
- Guide while acting: say one short sentence, then act; narrate what's on screen.
- Ask only for required missing info; read back key fields before Save; **never** say "saved" until `save()` returns ok.
- Handle corrections at any time (re-fill field), questions midway (answer, then resume), unsupported requests (say so, offer what is possible).
- Keep utterances short (voice).

## Env
Read by `app/config.py` (real environment variables win over `.env`).

- Required: `GEMINI_API_KEY` (or `GOOGLE_API_KEY`).
- Model and CRM: `GEMINI_LIVE_MODEL` (optional; empty = auto-pick the best available Live/native-audio model with
  fallback), `ESPO_URL=https://demo.us.espocrm.com`.
- Capacity and lifetime: `MAX_SESSIONS` (default 3; 1 when `RENDER` is set), `IDLE_TIMEOUT_S=180` (end a session
  after this long without user audio/text), `SESSION_MAX_MINUTES=15`, `TOOL_TIMEOUT_S=50`.
- Host: `PORT=7860`, `HEADLESS=1`, `LOW_MEMORY=0` (1 on small instances: lazy Chromium launch/close, low-memory
  flags, lighter screencast), `ALLOWED_ORIGINS` (comma-separated Origin allow-list for `/ws`; empty = the page's
  own host only), `METRICS_PATH=data/metrics.jsonl`.
- Voice tuning: `LIVE_VOICE=Kore`, `VAD_START_SENSITIVITY=high`, `VAD_END_SENSITIVITY=high`,
  `VAD_PREFIX_PADDING_MS=200`, `VAD_SILENCE_DURATION_MS=600`, `LIVE_ASYNC_TOOLS=0` (NON_BLOCKING tool declarations,
  model dependent), `LIVE_TEXT_MODE=auto` (`auto` | `client_content` | `realtime`).

`GEMINI_TEXT_MODEL` was removed: evals run against the Live model in typed-text mode.
