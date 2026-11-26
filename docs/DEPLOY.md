# Deploying to Render (free tier)

The app ships as one Docker web service: FastAPI + uvicorn (single worker), a shared
Playwright Chromium (headless shell) and the Gemini Live API. It is tuned for Render's
free instance: **512 MB RAM, 0.1 CPU, sleeps after 15 minutes without traffic**, and
serves **one voice session at a time** (`MAX_SESSIONS=1`).

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Python 3.12 slim, deps from `uv.lock` (no dev group), Chromium headless shell only, non-root user |
| `.dockerignore` | Keeps `.env`, tests, evals, docs and local data out of the image |
| `render.yaml` | Blueprint: one free Docker web service in Singapore, health check `/healthz` |
| `scripts/deploy_render.py` | Create-or-update the service through the Render REST API, deploy, wait, check `/healthz` |
| `.github/workflows/keepwarm.yml` | Optional pinger, off unless enabled by repository variables |

## Runtime settings

| Variable | Value on Render | Effect |
|---|---|---|
| `GEMINI_API_KEY` | secret | Gemini Live access |
| `GEMINI_LIVE_MODEL` | `gemini-3.1-flash-live-preview` | Pins the Live model |
| `RENDER` | `1` | Defaults `MAX_SESSIONS` to 1 |
| `MAX_SESSIONS` | `1` | Extra sessions get a "busy" message instead of an out-of-memory crash |
| `LOW_MEMORY` | `1` | Chromium low-memory flags (one renderer process, no site isolation, smaller V8 heap); see `app/crm/browser.py` |
| `HEADLESS` | `1` | Headless shell (the only browser installed in the image) |
| `PORT` | set by Render | uvicorn binds to it |

## One-time setup (owner)

1. Push the repository to GitHub (`main` branch).
2. Create a Render account (sign in with GitHub is simplest) and allow Render's GitHub app
   to access the repository (Account settings -> GitHub, or during the first "New" flow).
3. Pick one way to create the service:
   - **Blueprint:** Dashboard -> New -> Blueprint -> select the repo. Render reads
     `render.yaml` and asks for `GEMINI_API_KEY` (marked `sync: false`).
   - **API script:** Account settings -> API Keys -> create a key, add
     `RENDER_API_KEY=...` to the local `.env` (never commit it), then:
     ```bash
     uv run python scripts/deploy_render.py --dry-run   # review payloads, secrets redacted
     uv run python scripts/deploy_render.py             # create/update + deploy + health check
     ```
     The script finds the service by name (`voice-crm-agent`), creates it if missing
     (free plan, Singapore, Docker), otherwise updates it and replaces its env vars,
     triggers a deploy, polls until `live`, prints the `onrender.com` URL and calls `/healthz`.
4. Open the printed URL, allow the microphone, start a session.

Later pushes to `main` redeploy automatically (`autoDeployTrigger: commit`).

## Before a review: warm it up

A sleeping free instance needs roughly 30-60 s to wake (container start + Chromium
launch). About 2 minutes before the review:

1. Open `https://<service>.onrender.com/healthz` and wait for
   `{"ok": true, "browser": true, ...}`.
2. Open the app page and keep it open; the instance stays awake while it receives traffic.

### Optional: keep it awake with GitHub Actions

`.github/workflows/keepwarm.yml` curls `/healthz` every 10 minutes. It does nothing until
you add two repository variables (Settings -> Secrets and variables -> Actions -> Variables):

- `KEEPWARM_ENABLED` = `true`
- `KEEPWARM_URL` = `https://<service>.onrender.com`

Notes: GitHub cron is best effort (runs can be delayed by several minutes), and an
always-on instance consumes about 720 of the 750 free instance hours per month. Turn it
off after the review by deleting `KEEPWARM_ENABLED` or setting it to `false`. Any external
uptime pinger pointed at `/healthz` works the same way.

## Local container check

```bash
docker build -t crm-voice-guide .
docker run --rm -p 7860:7860 --memory=512m --env-file .env -e LOW_MEMORY=1 crm-voice-guide
curl http://localhost:7860/healthz
```

## Limits and risks

- **Memory:** 512 MB covers uvicorn + one Chromium context. A second concurrent session is
  refused by design. If Render reports out-of-memory restarts, reduce screencast quality
  or the viewport, or move to the paid 2 GB instance.
- **CPU:** 0.1 CPU makes page loads and the CRM login slower than locally (several
  seconds). JPEG screencast encoding also uses CPU.
- **Cold start:** the first visitor after 15 idle minutes waits for the wake-up.
- **Ephemeral disk:** `data/metrics.jsonl` resets on every deploy or restart.
