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
  config.py        env settings (GEMINI_API_KEY, GEMINI_LIVE_MODEL, MAX_SESSIONS, ESPO_URL, HEADLESS, PORT=7860)
  server.py        FastAPI: GET / → static/index.html; /static; WS /ws; GET /healthz; GET /api/metrics
  session.py       orchestrates one reviewer session: client WS ⇄ live ⇄ tools ⇄ browser; emits events
  live.py          Gemini Live wrapper (google-genai SDK, client.aio.live.connect)
  prompts.py       system instruction (built from ui_map.json)
  metrics.py       per-turn/per-tool timings, task outcomes → JSONL (data/metrics.jsonl) + summary
  crm/browser.py   CrmBrowser: launch/pool, new context per session, login, screencast, highlight
  crm/tools.py     TOOL_DECLARATIONS (Gemini function declarations) + async implementations
  crm/ui_map.py    load/query ui_map.json
crawler/crawl.py   crawls EspoCRM → data/ui_map.json (entities, fields, labels, types, enum options, actions)
evals/run_evals.py scripted multi-turn scenarios (text mode) × N runs → evals/report.md + json
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
`GEMINI_API_KEY` (required), `GEMINI_LIVE_MODEL` (optional; else auto-pick first available live/native-audio model),
`GEMINI_TEXT_MODEL` (evals), `ESPO_URL=https://demo.us.espocrm.com`, `MAX_SESSIONS=3`, `HEADLESS=1`, `PORT=7860`.
