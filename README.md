# Voice CRM Agent

**Talk to a CRM.** This is a voice agent that operates the live EspoCRM demo in a real browser: you say "add a
contact named Maya Okafor", and it opens the form, fills it while you watch, reads it back, and saves. Interrupt it
anytime (correct a name before Save, ask a question midway, or change your mind) and it adapts without losing the
form. It only says "saved" after reading the result back from the page, and every run is measured: latency, task
completion and false-success claims across scripted multi-turn conversations.

**Live demo:** https://voice-crm-agent.onrender.com · **Technical notes (PDF):** [docs/TECHNICAL_NOTES.pdf](docs/TECHNICAL_NOTES.pdf) ·
**Eval report:** [evals/REPORT.md](evals/REPORT.md)

> **About the hosted demo.** It runs on a free instance (0.1 CPU, 512 MB) that sleeps when idle, so the first visit
> takes about a minute to wake up, a session takes ~30 s to open the CRM, and each CRM action takes 8–11 s. Measured
> on a normal machine the same build opens a session in ~6 s, fills a form in ~0.5–2 s and saves in ~2.5 s (see the
> eval report). Chrome or Edge with headphones gives the best experience; one visitor at a time.

Built with the Gemini Live API (`gemini-3.1-flash-live-preview`, speech-to-speech), Playwright and FastAPI.

## Try it in 30 seconds

Open the demo, click **Start call**, allow the microphone (or type in the message box), then say:

1. *"Add a new contact, John Miller, email john.miller@example.com."* The form opens and fills on the left.
2. While it reads the name back, cut in: *"Actually it's Jon, J-O-N, no h."* It stops talking, changes only the
   first name and confirms "Jon".
3. *"What's the difference between a contact and an account?"* A two-sentence answer; the form is untouched.
4. *"Save it."* The detail page appears; the agent quotes a value from it. The action log on the right shows
   `save` returning `ok` before the agent says "saved".

Also try: name any opportunity from the Opportunities board and say *"move it to Negotiation"*; *"Delete all
contacts"* (it declines); or *"Add Maya"* (it asks one clarifying question first).

The free host sleeps when idle: the first visit can take 30–60 s to wake up, and it serves one session at a time.

## What it does and how

```
browser (mic, speaker, live view, transcript)
   |  one WebSocket: PCM16 audio up / agent audio, JPEG frames, JSON events down
FastAPI session (one per visitor)
   |-- Gemini Live: speech in, speech out, server-side VAD, barge-in, tool calls
   '-- verified CRM tools --> Playwright Chromium (own context per session) --> EspoCRM demo
```

- **Speech-to-speech** over one Live connection: low latency and built-in barge-in instead of an STT → LLM → TTS
  chain.
- **High-level, verified tools** (`open_create_form`, `fill_fields`, `save`, `set_stage`, ...). Each acts visibly
  (highlight, human-paced typing) and returns an **observation read back from the DOM**: field values after
  typing, the CRM's validation messages, the saved record's detail view.
- **Never claims success early:** "saved" only after `save` returns `ok: true` (detail view with an id *and* a 2xx
  write); enforced by the tool, the prompt and the eval harness.
- **Crawler → UI map:** fields, labels, widgets, required flags and enum options come from crawling the CRM, and
  feed both the prompt and argument validation.

## The three reviewer tests

| Test | What makes it work |
|---|---|
| **Guided workflow**: talk, and it navigates while it explains | Announce → act → narrate policy; visible typing and highlights; JPEG screencast of the agent's browser; action timeline with timings |
| **Saved result on screen** | `save` waits for the detail view and a successful write, then returns the values shown there; the agent quotes one of them |
| **Interruptions**: correct a name, ask midway | Gemini VAD with `START_OF_ACTIVITY_INTERRUPTS` → `interrupted` event → queued audio dropped on the server and the client's playback buffer flushed; corrections re-fill one field on the same form; Save is never cancelled half-way |

## Bonus items

- **Learns the interface by crawling:** `crawler/crawl.py` logs in, opens each entity's create form, list and
  detail views, and writes `data/ui_map.json` (fields, labels, widgets, required flags, enum options, actions).
  The prompt's field list and the tools' field/option resolution are built from it.
  `uv run python -m crawler.crawl [--entities Contact,Opportunity]`
- **Measured across runs:** `evals/run_evals.py` runs 20 scripted multi-turn conversations (happy path,
  corrections, barge-in, questions, change of direction, unsupported and vague requests, validation errors, the
  opportunity workflow) against the real model and the real demo, and verifies the end state through the CRM API.
  Latest results ([evals/REPORT.md](evals/REPORT.md)): **97.2% pass (35/36 runs)**, 0 false-success claims, first audio 0.88 s p50 on talk-only turns and 3.4 s p50 on turns that run a CRM action.

## Run locally

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and a free Gemini API key.

```bash
uv sync
uv run playwright install chromium
cp .env.example .env            # then set GEMINI_API_KEY=...
uv run uvicorn app.server:app --port 7860
```

Open http://localhost:7860 and click **Start call**. Useful settings (all optional, see `app/config.py`):
`GEMINI_LIVE_MODEL` (pin a model; default auto-selects with fallback), `MAX_SESSIONS` (default 3),
`IDLE_TIMEOUT_S` (180), `LOW_MEMORY` (lazy Chromium for small hosts), `ALLOWED_ORIGINS`, `HEADLESS`.

Endpoints: `GET /healthz`, `GET /api/metrics` (latency percentiles, tool latency, completion, interventions).

## Tests

```bash
uv run pytest -m "not live"     # unit + session-flow tests with fakes, no network (seconds)
uv run pytest -m live           # tool workflows against the public demo in real Chromium (about a minute)
```

## Evals

```bash
uv run python -m evals.run_evals                              # full catalogue, 1 run each
uv run python -m evals.run_evals --runs 3                     # repeat for variance
uv run python -m evals.run_evals --scenarios S01,S03 --runs 5
uv run python -m evals.run_evals --tags happy_path,correction
uv run python -m evals.run_evals --url ws://127.0.0.1:7860/ws # use a running server
uv run python -m evals.run_evals --report-from evals/results/a.json,evals/results/b.json
```

The harness starts the server, sends user lines as typed text (mic off) so runs are reproducible, puts a unique
run id into every email and record name, checks each turn (forbidden tools, premature success claims, claimed
actions without a tool call) and the final CRM state, then deletes what it created. Results go to `evals/results/`
and the report to `evals/REPORT.md`. Do not run evals against the deployed key while reviewers are testing: they
share the free quota.

## Deploy

One Docker service on a free tier (512 MB RAM, one session at a time, lazy browser). See
[docs/DEPLOY.md](docs/DEPLOY.md) for the Render blueprint, the API deploy script and the warm-up checklist.

## Repository layout

```
app/
  server.py        FastAPI: UI, WS /ws, /healthz, /api/metrics; session limit, Origin check
  session.py       one visitor's session: client WS <-> Gemini Live <-> tools <-> browser; metrics
  live.py          Gemini Live wrapper: config, VAD, model fallback, resume on disconnect
  prompts.py       conversation policy + field list generated from the UI map
  metrics.py       JSONL events and aggregate summary
  config.py        environment settings
  crm/browser.py   shared Chromium, isolated context per session, login, screencast, highlight
  crm/tools.py     verified tools and their function declarations
  crm/ui_map.py    UI map loading; field, entity and option resolution
crawler/crawl.py   EspoCRM crawler -> data/ui_map.json
evals/             scenarios.json, run_evals.py, REPORT.md, results/
static/            index.html, app.js, audio-worklet.js (mic capture, playback ring buffer), styles.css
tests/             unit, session-flow (fakes) and live tool tests
docs/              ARCHITECTURE.md, REQUIREMENTS.md, DEPLOY.md, TECHNICAL_NOTES.md (+ PDF, build_pdf.py)
```

## Limitations

- Evals are text-mode: they do not measure speech-recognition errors on names and emails, the most likely real
  failure. Voice-mode evals are the next step.
- No filler speech while a slow tool runs: tool calls are blocking, so a slow demo response means a few seconds of
  silence (the action log shows the step running).
- The public demo is shared: latency varies, data resets, other visitors can edit records.
- Preview model: behaviour and quotas can change; results are averages over non-deterministic runs.
- The free host serves one session at a time; the live view is a JPEG screencast, not WebRTC.
- The crawler maps forms and fields, not multi-step flows or permissions.

More detail, including design trade-offs and failure tracing, in the
[technical notes](docs/TECHNICAL_NOTES.md). Author: [nguyenduykhoa2601](https://github.com/nguyenduykhoa2601)
