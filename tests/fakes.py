"""Test doubles for the Gemini Live session, the CRM browser and the client socket."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from google.genai import errors, types

_CLOSE = object()


class FakeLiveSession:
    """Mimics google.genai.live.AsyncSession for the methods LiveAgent uses."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.audio: list[types.Blob] = []
        self.texts: list[str] = []
        self.client_contents: list[Any] = []
        self.tool_responses: list[list[types.FunctionResponse]] = []
        self.audio_stream_ends = 0
        self.closed = False

    # --- client → server
    async def send_realtime_input(self, *, audio=None, audio_stream_end=None, text=None, **_: Any):
        if audio is not None:
            self.audio.append(audio)
        if audio_stream_end:
            self.audio_stream_ends += 1
        if text is not None:
            self.texts.append(text)

    async def send_client_content(self, *, turns=None, turn_complete=True):
        self.client_contents.append(turns)

    async def send_tool_response(self, *, function_responses):
        if not isinstance(function_responses, list):
            function_responses = [function_responses]
        self.tool_responses.append(function_responses)

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(_CLOSE)

    # --- server → client (same semantics as the SDK: ends after turn_complete)
    async def receive(self):
        while True:
            msg = await self.inbox.get()
            if msg is _CLOSE:
                raise errors.APIError(1000, {"message": "closed"})
            yield msg
            if msg.server_content is not None and msg.server_content.turn_complete:
                return

    # --- helpers for tests
    def push(self, msg: types.LiveServerMessage) -> None:
        self.inbox.put_nowait(msg)

    def drop_connection(self, code: int = 1011) -> None:
        self.inbox.put_nowait(_CLOSE if code == 1000 else _Drop(code))


class _Drop:
    def __init__(self, code: int) -> None:
        self.code = code


class FakeConnector:
    """Replacement for client.aio.live.connect that hands out FakeLiveSessions."""

    def __init__(self, fail_models: set[str] | None = None) -> None:
        self.sessions: list[FakeLiveSession] = []
        self.configs: list[types.LiveConnectConfig] = []
        self.models: list[str] = []
        self.fail_models = fail_models or set()
        self.new_session = asyncio.Event()

    @contextlib.asynccontextmanager
    async def connect(self, *, model: str, config: types.LiveConnectConfig):
        self.models.append(model)
        if model in self.fail_models:
            raise errors.APIError(1008, {"message": f"model {model} not found"})
        self.configs.append(config)
        session = FakeLiveSession()
        original_receive = session.receive

        async def receive():
            async for msg in original_receive():
                if isinstance(msg, _Drop):
                    raise errors.APIError(msg.code, {"message": "dropped"})
                yield msg

        session.receive = receive  # type: ignore[method-assign]
        self.sessions.append(session)
        self.new_session.set()
        try:
            yield session
        finally:
            session.closed = True


# ----------------------------------------------------------------- message builders


def audio_msg(pcm: bytes) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                role="model",
                parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))],
            )
        )
    )


def out_text_msg(text: str, finished: bool = False) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text=text, finished=finished)
        )
    )


def in_text_msg(text: str, finished: bool = False) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text=text, finished=finished)
        )
    )


def interrupted_msg() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))


def turn_complete_msg() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


def tool_call_msg(*calls: tuple[str, str, dict[str, Any]]) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[types.FunctionCall(id=i, name=n, args=a) for i, n, a in calls]
        )
    )


def cancel_msg(*ids: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        tool_call_cancellation=types.LiveServerToolCallCancellation(ids=list(ids))
    )


def resumption_msg(handle: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(
            new_handle=handle, resumable=True
        )
    )


def go_away_msg(time_left: str = "10s") -> types.LiveServerMessage:
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left=time_left))


# ----------------------------------------------------------------- CRM + transport


class FakeCrm:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, frames: list[bytes] | None = None) -> None:
        self.frames = frames or []
        self.crm = FakeCrm()
        self.on_frame = None

    async def new_session(self, on_frame):
        self.on_frame = on_frame
        for f in self.frames:
            await on_frame(f)
        return self.crm


class FakeTransport:
    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.binary: list[bytes] = []
        self.gate: asyncio.Event | None = None  # when set, sends block until it opens
        self.closed = False

    async def send_text(self, data: str) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.texts.append(json.loads(data))

    async def send_bytes(self, data: bytes) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.binary.append(data)

    async def close(self) -> None:
        self.closed = True

    def events(self, type_: str) -> list[dict[str, Any]]:
        return [e for e in self.texts if e.get("type") == type_]


async def wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)
