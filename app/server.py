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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, settings
from app.metrics import get_store
from app.session import Session

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("app.server")

STATIC_DIR = PROJECT_ROOT / "static"


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
state: dict[str, Any] = {"browser": None, "browser_error": None}


async def _start_browser() -> None:
    try:
        from app.crm.browser import CrmBrowser

        browser = CrmBrowser()
        start = getattr(browser, "start", None)
        if callable(start):
            await start()
        state["browser"] = browser
        log.info("CRM browser ready")
    except Exception as exc:
        state["browser_error"] = str(exc)
        log.exception("CRM browser failed to start")


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
                stop = getattr(browser, "shutdown", None) or browser.close
                await asyncio.wait_for(stop(), 15)
        state["browser"] = None


app = FastAPI(title="Voice CRM Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


@app.get("/", response_model=None)
async def index() -> FileResponse | HTMLResponse:
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("<h1>Voice CRM Agent</h1><p>UI not built yet.</p>")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "api_key": settings.has_api_key,
        "browser": state.get("browser") is not None,
        "browser_error": state.get("browser_error"),
        "sessions": {"active": limiter.active, "max": limiter.limit},
    }


@app.get("/api/metrics")
async def api_metrics() -> JSONResponse:
    summary = await asyncio.to_thread(get_store().summary)
    return JSONResponse(summary)


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
    await ws.accept()
    session = Session(
        _WsTransport(ws),
        browser=state.get("browser"),
        acquire_slot=limiter.acquire,
        release_slot=limiter.release,
    )
    session.start_writer()
    try:
        while not session.closed:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.handle_client_audio(message["bytes"])
            elif message.get("text") is not None:
                if not await session.handle_client_text(message["text"]):
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("[%s] websocket error", session.id)
    finally:
        await session.shutdown()
        with contextlib.suppress(Exception):
            await ws.close()


def main() -> None:
    import uvicorn

    uvicorn.run("app.server:app", host="0.0.0.0", port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
