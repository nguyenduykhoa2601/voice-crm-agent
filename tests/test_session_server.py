"""HTTP/WS smoke tests for the FastAPI app (lifespan not run: no browser, no network)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

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
