"""FastAPI entry point.

Routes: GET / (UI), /static, WS /ws (voice session), GET /healthz, GET /api/metrics.
Run with `python -m app.server` or `uvicorn app.server:app --port 7860`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, settings
from app.metrics import get_store
from app.session import Session

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("app.server")

STATIC_DIR = PROJECT_ROOT / "static"

# /ws abuse limits. Real clients send ~2-4 KB mic chunks and short JSON messages.
START_DEADLINE_S = 15.0  # close sockets that never send {"type": "start"}
MAX_BINARY_BYTES = 64 * 1024
MAX_TEXT_CHARS = 8 * 1024


class SessionLimiter:
    """Non-blocking counter for concurrently active sessions."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0

    def acquire(self) -> bool:
        if self.active >= self.limit:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


limiter = SessionLimiter(settings.max_sessions)
state: dict[str, Any] = {"browser": None, "browser_error": False}


async def _start_browser() -> None:
    """Create the shared browser. Chromium itself is (re)launched by
    `CrmBrowser.new_session()`, so a failed warm-up here is retried on the next session."""
    try:
        from app.crm.browser import CrmBrowser

        browser = CrmBrowser(base_url=settings.espo_url, headless=settings.headless)
    except Exception:
        state["browser_error"] = True
        log.exception("CRM browser could not be created")
        return
    state["browser"] = browser
    if settings.low_memory:
        # On small instances Chromium is launched on the first session and
        # closed again when the last one ends (see _release_browser_if_idle).
        log.info("CRM browser created (lazy start: LOW_MEMORY=1)")
        return
    try:
        await browser.start()
        log.info("CRM browser ready")
    except Exception:
        state["browser_error"] = True
        log.exception("CRM browser warm-up failed; it will be retried on the next session")


async def _release_browser_if_idle() -> None:
    """Free Chromium's memory between sessions on small instances."""
    browser = state.get("browser")
    if not settings.low_memory or browser is None:
        return
    if limiter.active == 0 and browser.session_count == 0:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(browser.close(), 15)
            log.info("CRM browser closed (idle)")


async def _warm_model_cache() -> None:
    if not settings.has_api_key:
        log.warning("GEMINI_API_KEY is not set: sessions will report an error on start")
        return
    try:
        from app.live import candidate_models, make_client

        await candidate_models(make_client(settings.gemini_api_key), settings.gemini_live_model)
    except Exception as exc:
        log.warning("model warm-up failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.gather(_start_browser(), _warm_model_cache())
    try:
        yield
    finally:
        browser = state.get("browser")
        if browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), 15)
        state["browser"] = None


app = FastAPI(title="Voice CRM Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "api_key": settings.has_api_key,
        "browser": state.get("browser") is not None,
        # Details are in the server log only (exception text can contain paths).
        "browser_error": bool(state.get("browser_error")),
        "sessions": {"active": limiter.active, "max": limiter.limit},
    }


@app.get("/api/metrics")
async def api_metrics() -> JSONResponse:
    summary = await asyncio.to_thread(get_store().cached_summary)
    return JSONResponse(summary)


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """Cross-site WebSocket guard. Browsers always send Origin; other clients
    (tests, evals) may omit it and are allowed."""
    if not origin:
        return True
    origin = origin.rstrip("/")
    if settings.allowed_origins:
        return origin in settings.allowed_origins
    return urlsplit(origin).netloc == (host or "")


class _WsTransport:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def send_text(self, data: str) -> None:
        await self.ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def close(self) -> None:
        await self.ws.close()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host")):
        log.warning("rejected /ws from origin %s", ws.headers.get("origin"))
        await ws.close(code=1008)
        return
    await ws.accept()
    session = Session(
        _WsTransport(ws),
        browser=state.get("browser"),
        acquire_slot=limiter.acquire,
        release_slot=limiter.release,
    )
    session.start_writer()
    start_deadline = asyncio.get_running_loop().time() + START_DEADLINE_S
    try:
        while not session.closed:
            timeout = None
            if not session.started:
                timeout = max(0.0, start_deadline - asyncio.get_running_loop().time())
            message = await asyncio.wait_for(ws.receive(), timeout)
            if message.get("type") == "websocket.disconnect":
                break
            data, text = message.get("bytes"), message.get("text")
            if (data is not None and len(data) > MAX_BINARY_BYTES) or (
                text is not None and len(text) > MAX_TEXT_CHARS
            ):
                log.warning("[%s] message too large; closing", session.id)
                await ws.close(code=1009)
                break
            if data is not None:
                await session.handle_client_audio(data)
            elif text is not None:
                if not await session.handle_client_text(text):
                    break
    except asyncio.TimeoutError:
        log.info("[%s] no start message within %.0fs; closing", session.id, START_DEADLINE_S)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("[%s] websocket error", session.id)
    finally:
        await session.shutdown()
        with contextlib.suppress(Exception):
            await ws.close()
        await _release_browser_if_idle()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=settings.port,
        log_level="info",
        ws_max_size=1 << 20,  # uvicorn's default is 16 MiB
    )


if __name__ == "__main__":
    main()
