"""HTTP/WS smoke tests for the FastAPI app (lifespan not run: no browser, no network)."""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import server


def test_http_routes():
    client = TestClient(server.app)
    assert client.get("/").status_code == 200
    health = client.get("/healthz").json()
    assert health["ok"] is True and "sessions" in health
    metrics = client.get("/api/metrics").json()
    assert "first_audio_ms" in metrics and "task_completion" in metrics


def test_ws_start_without_browser_reports_error_and_ends():
    client = TestClient(server.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start"})
        seen = []
        while True:
            msg = ws.receive_json()
            seen.append(msg)
            if msg["type"] == "status" and msg["state"] in ("ended", "busy"):
                break
    assert any(m["type"] == "error" for m in seen)
    # The app runs in another thread; give its shutdown a moment to release the slot.
    deadline = time.monotonic() + 2
    while server.limiter.active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.limiter.active == 0


def test_limiter():
    lim = server.SessionLimiter(1)
    assert lim.acquire() and not lim.acquire()
    lim.release()
    assert lim.acquire()


def _read_until_closed(ws) -> int:
    """Read messages until the server closes the socket; returns the close code."""
    try:
        while True:
            ws.receive_json()
    except WebSocketDisconnect as exc:
        return exc.code


def test_ws_rejects_foreign_origin():
    client = TestClient(server.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    # Same host is allowed.
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"type": "stop"})


def test_ws_closes_socket_that_never_starts(monkeypatch):
    monkeypatch.setattr(server, "START_DEADLINE_S", 0.2)
    client = TestClient(server.app)
    with client.websocket_connect("/ws") as ws:
        _read_until_closed(ws)  # returns instead of hanging


def test_ws_closes_on_oversized_message():
    client = TestClient(server.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"\0" * (server.MAX_BINARY_BYTES + 1))
        assert _read_until_closed(ws) == 1009


def test_browser_warm_up_failure_keeps_browser_for_retry(monkeypatch):
    import app.crm.browser as browser_mod

    class FailingBrowser:
        def __init__(self, **_):
            pass

        async def start(self):
            raise RuntimeError("chromium crashed at /some/path")

    monkeypatch.setattr(browser_mod, "CrmBrowser", FailingBrowser)
    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, low_memory=False))
    monkeypatch.setattr(server, "state", {"browser": None, "browser_error": False})
    asyncio.run(server._start_browser())
    # The object is kept, so the next session's new_session() launches Chromium again.
    assert isinstance(server.state["browser"], FailingBrowser)
    health = TestClient(server.app).get("/healthz").json()
    assert health["browser_error"] is True  # a boolean: no exception text leaks
