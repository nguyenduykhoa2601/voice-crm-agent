"""Gemini Live API wrapper (google-genai SDK, `client.aio.live.connect`).

`LiveAgent` owns one Live connection for one reviewer session:
- builds the `LiveConnectConfig` (audio out, transcriptions, VAD, compression, resumption, tools)
- runs the receive loop and dispatches events to a `LiveEvents` handler (session.py)
- reconnects transparently on `go_away` / dropped connections using the latest
  session-resumption handle
- falls back to the next candidate model if the first one cannot be connected

Model choice: GEMINI_LIVE_MODEL if set; otherwise `models.list()` filtered on the
"bidiGenerateContent" action (native-audio preferred, newest first); otherwise a
hard-coded candidate list that is probed by connecting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any, AsyncContextManager, Callable, Protocol, Sequence

from google import genai
from google.genai import errors, types

log = logging.getLogger(__name__)

LIVE_API_VERSION = "v1beta"  # the Live endpoint is not served on "v1"

# Probed in order when the model list does not expose Live models.
CANDIDATE_MODELS: tuple[str, ...] = (
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-2.5-flash-native-audio-preview-09-2025",
)

INPUT_AUDIO_MIME = "audio/pcm;rate=16000"

# WebSocket close codes worth a resume/reconnect (normal close, going away,
# abnormal closure, internal error, try again later, bad gateway).
RETRYABLE_CLOSE_CODES = {1000, 1001, 1006, 1011, 1012, 1013, 1014}
# Codes that indicate the model/config itself was rejected (try another model).
MODEL_REJECTED_CODES = {404, 1007, 1008}

MAX_RECONNECTS = 5


def make_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version=LIVE_API_VERSION),
    )


# --------------------------------------------------------------------------- model selection


# Live-capable models that are not general voice-agent models.
_EXCLUDED_MARKERS = ("translate", "transcribe", "robotics", "genie", "music", "lyria")


def _model_rank(name: str) -> tuple[int, int, float, int]:
    """Sort key (higher is better): (audio-native, not-extended-thinking, version, date)."""
    short = name.split("/")[-1]
    audio_native = 1 if ("native-audio" in short or "live" in short) else 0
    # "...-thinking" variants need extra config and add latency: try them last.
    fast = 0 if "thinking" in short else 1
    m = re.search(r"gemini-(?:live-)?(\d+(?:\.\d+)?)", short)
    version = float(m.group(1)) if m else 0.0
    date = 0
    d = re.search(r"(\d{2})-(\d{4})$", short)  # e.g. ...-12-2025
    if d:
        date = int(d.group(2)) * 100 + int(d.group(1))
    elif short.endswith("-latest"):
        date = 999999
    return (audio_native, fast, version, date)


def rank_live_models(models: Sequence[Any]) -> list[str]:
    """Names of models that support bidiGenerateContent, best first."""
    names: list[str] = []
    for m in models:
        actions = [a.lower() for a in (getattr(m, "supported_actions", None) or [])]
        name = getattr(m, "name", None) or ""
        short = name.split("/")[-1]
        if (
            "bidigeneratecontent" in actions
            and short.startswith("gemini")
            and not any(x in short for x in _EXCLUDED_MARKERS)
        ):
            names.append(short)
    return sorted(set(names), key=_model_rank, reverse=True)


_model_cache: list[str] | None = None
_model_lock = asyncio.Lock()


async def candidate_models(client: genai.Client, override: str = "") -> list[str]:
    """Ordered list of models to try. Cached for the process lifetime."""
    global _model_cache
    if override:
        return [override]
    async with _model_lock:
        if _model_cache is not None:
            return list(_model_cache)
        listed: list[str] = []
        try:
            pager = await client.aio.models.list()
            all_models = [m async for m in pager]
            listed = rank_live_models(all_models)
        except Exception as exc:
            log.warning("models.list() failed: %s", exc)
        ordered = listed + [m for m in CANDIDATE_MODELS if m not in listed]
        log.info("live model candidates: %s (listed: %s)", ordered, listed or "none")
        _model_cache = ordered
        return list(ordered)


def remember_working_model(model: str) -> None:
    """Move a model that connected successfully to the front of the cache."""
    global _model_cache
    if _model_cache is None:
        _model_cache = [model]
    elif _model_cache[0] != model:
        _model_cache = [model] + [m for m in _model_cache if m != model]


# --------------------------------------------------------------------------- config


def _start_sens(v: str) -> types.StartSensitivity:
    return (
        types.StartSensitivity.START_SENSITIVITY_LOW
        if v == "low"
        else types.StartSensitivity.START_SENSITIVITY_HIGH
    )


def _end_sens(v: str) -> types.EndSensitivity:
    return (
        types.EndSensitivity.END_SENSITIVITY_LOW
        if v == "low"
        else types.EndSensitivity.END_SENSITIVITY_HIGH
    )


def build_function_declarations(
    tool_declarations: Sequence[dict[str, Any]], non_blocking: bool
) -> list[types.FunctionDeclaration]:
    decls: list[types.FunctionDeclaration] = []
    for d in tool_declarations:
        if isinstance(d, types.FunctionDeclaration):
            fd = d
        else:
            fd = types.FunctionDeclaration(**d)
        if non_blocking:
            fd = fd.model_copy(update={"behavior": types.Behavior.NON_BLOCKING})
        decls.append(fd)
    return decls


def build_live_config(
    *,
    system_instruction: str,
    tool_declarations: Sequence[dict[str, Any]],
    voice: str = "Kore",
    start_sensitivity: str = "high",
    end_sensitivity: str = "high",
    prefix_padding_ms: int = 200,
    silence_duration_ms: int = 600,
    non_blocking_tools: bool = False,
    resumption_handle: str | None = None,
) -> types.LiveConnectConfig:
    tools = []
    if tool_declarations:
        tools = [
            types.Tool(
                function_declarations=build_function_declarations(
                    tool_declarations, non_blocking_tools
                )
            )
        ]
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        system_instruction=types.Content(
            role="system", parts=[types.Part(text=system_instruction)]
        ),
        tools=tools or None,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=_start_sens(start_sensitivity),
                end_of_speech_sensitivity=_end_sens(end_sensitivity),
                prefix_padding_ms=prefix_padding_ms,
                silence_duration_ms=silence_duration_ms,
            ),
            # User speech interrupts the agent (barge-in).
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
    )


def uses_realtime_text(model: str, mode: str = "auto") -> bool:
    """Whether typed text goes through realtime_input.text (vs client_content).

    Gemini 3.x Live models only accept client_content for seeding initial history,
    so text during the conversation must use realtime input. 2.5 models accept both.
    """
    if mode == "realtime":
        return True
    if mode == "client_content":
        return False
    return _model_rank(model)[2] >= 3.0


# --------------------------------------------------------------------------- agent


class LiveEvents(Protocol):
    async def on_audio(self, pcm: bytes) -> None: ...
    async def on_transcript(self, role: str, text: str, final: bool) -> None: ...
    async def on_interrupted(self) -> None: ...
    async def on_turn_complete(self) -> None: ...
    async def on_tool_call(self, calls: list[types.FunctionCall]) -> None: ...
    async def on_tool_cancel(self, ids: list[str]) -> None: ...
    async def on_usage(self, usage: types.UsageMetadata) -> None: ...
    async def on_voice_activity(self, kind: str) -> None: ...
    async def on_status(self, state: str, message: str) -> None: ...


ConnectFn = Callable[..., AsyncContextManager[Any]]


class LiveError(RuntimeError):
    pass


class LiveAgent:
    """One Gemini Live conversation with automatic resume/reconnect."""

    def __init__(
        self,
        *,
        connect: ConnectFn,
        models: Sequence[str],
        config_factory: Callable[[str | None], types.LiveConnectConfig],
        events: LiveEvents,
        text_mode: str = "auto",
    ) -> None:
        self._connect = connect
        self._models = list(models)
        self._config_factory = config_factory
        self._events = events
        self._text_mode = text_mode

        self.model: str | None = None
        self._session: Any = None
        self._ready = asyncio.Event()
        self._connected_once = asyncio.Event()
        self._closed = False
        self._task: asyncio.Task | None = None
        self._connect_error: BaseException | None = None

        self._handle: str | None = None
        self._reconnect_requested = False
        self._reconnect_deadline: float | None = None
        self._tools_in_flight = 0
        self._agent_speaking = False

        # Transcription accumulators (events are incremental; we emit deltas).
        self._out_text_open = False
        self._in_text_open = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self, timeout: float = 20.0) -> str:
        """Connect (trying candidate models) and start the receive loop.

        Returns the connected model name; raises LiveError if none could connect.
        """
        self._task = asyncio.create_task(self._run(), name="live-agent")
        waiter = asyncio.create_task(self._connected_once.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, self._task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            waiter.cancel()
            await self.close()  # never leave a half-open connection behind
            raise
        if waiter not in done:
            waiter.cancel()
            if self._task.done():
                exc = self._task.exception() if not self._task.cancelled() else None
                raise LiveError(f"Could not connect to the Live API: {exc or self._connect_error}")
            await self.close()
            raise LiveError("Timed out connecting to the Live API")
        assert self.model is not None
        return self.model

    async def close(self) -> None:
        self._closed = True
        self._ready.clear()
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def done_task(self) -> asyncio.Task | None:
        return self._task

    # ------------------------------------------------------------------ sending

    async def send_audio(self, pcm: bytes) -> bool:
        """Realtime mic audio. Dropped (returns False) while (re)connecting."""
        session = self._session
        if session is None or not self._ready.is_set():
            return False
        try:
            await session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=INPUT_AUDIO_MIME)
            )
            return True
        except Exception as exc:
            log.debug("send_audio failed (reconnecting?): %s", exc)
            return False

    async def end_audio_stream(self) -> None:
        session = await self._wait_session(timeout=2.0)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.send_realtime_input(audio_stream_end=True)

    async def send_text(self, text: str) -> None:
        session = await self._wait_session()
        if session is None:
            raise LiveError("Live session is not connected")
        if uses_realtime_text(self.model or "", self._text_mode):
            await session.send_realtime_input(text=text)
        else:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=True,
            )

    async def send_tool_responses(self, responses: list[types.FunctionResponse]) -> None:
        if not responses:
            return
        session = await self._wait_session()
        if session is None:
            log.warning("dropping %d tool responses: not connected", len(responses))
            return
        await session.send_tool_response(function_responses=responses)

    def tool_started(self) -> None:
        self._tools_in_flight += 1

    def tool_finished(self) -> None:
        self._tools_in_flight = max(0, self._tools_in_flight - 1)

    async def _wait_session(self, timeout: float = 15.0) -> Any:
        if self._closed:
            return None
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self._session

    # ------------------------------------------------------------------ connection loop

    async def _run(self) -> None:
        reconnects = 0
        model_idx = 0
        while not self._closed:
            if self.model is None:
                if model_idx >= len(self._models):
                    raise LiveError(
                        f"No Live model could be connected (tried {self._models}): "
                        f"{self._connect_error}"
                    )
                model = self._models[model_idx]
            else:
                model = self.model
            config = self._config_factory(self._handle)
            try:
                async with self._connect(model=model, config=config) as session:
                    first = self.model is None
                    self.model = model
                    self._session = session
                    self._reconnect_requested = False
                    self._reconnect_deadline = None
                    self._ready.set()
                    if first:
                        remember_working_model(model)
                        log.info("Live connected: model=%s", model)
                        self._connected_once.set()
                    else:
                        log.info("Live resumed (handle=%s)", bool(self._handle))
                        await self._events.on_status("ready", "Reconnected")
                    reconnects = 0
                    await self._receive_loop(session)
            except asyncio.CancelledError:
                raise
            except errors.APIError as exc:
                self._connect_error = exc
                code = getattr(exc, "code", None)
                if self.model is None:
                    log.warning("Live connect failed for %s: %s", model, exc)
                    model_idx += 1
                    continue
                if code in RETRYABLE_CLOSE_CODES and self._handle and reconnects < MAX_RECONNECTS:
                    reconnects += 1
                    log.warning("Live connection closed (%s); resuming (%d)", code, reconnects)
                else:
                    raise
            except Exception as exc:  # network errors, handshake failures
                self._connect_error = exc
                if self.model is None:
                    log.warning("Live connect failed for %s: %s", model, exc)
                    model_idx += 1
                    continue
                if self._handle and reconnects < MAX_RECONNECTS:
                    reconnects += 1
                    log.warning("Live connection error (%s); resuming (%d)", exc, reconnects)
                else:
                    raise
            finally:
                self._ready.clear()
                self._session = None
            if self._closed:
                break
            if reconnects:
                await self._events.on_status("reconnecting", "Reconnecting to the voice service…")
                await asyncio.sleep(min(0.5 * reconnects, 2.0))

    async def _receive_loop(self, session: Any) -> None:
        """Process messages until the connection closes or a reconnect is due."""
        while not self._closed:
            # `receive()` yields messages until the end of one model turn.
            async for msg in session.receive():
                await self._handle_message(msg)
                if self._should_reconnect_now():
                    log.info("Live: reconnecting for go_away")
                    return
            if self._should_reconnect_now():
                return

    def _should_reconnect_now(self) -> bool:
        if not self._reconnect_requested or not self._handle:
            return False
        if self._reconnect_deadline is not None and time.monotonic() >= self._reconnect_deadline:
            return True
        return self._tools_in_flight == 0 and not self._agent_speaking

    # ------------------------------------------------------------------ dispatch

    async def _handle_message(self, msg: types.LiveServerMessage) -> None:
        if msg.session_resumption_update is not None:
            upd = msg.session_resumption_update
            if upd.resumable and upd.new_handle:
                self._handle = upd.new_handle

        if msg.go_away is not None:
            self._reconnect_requested = True
            left = _parse_duration_s(msg.go_away.time_left)
            # Leave a safety margin before the server cuts the connection.
            self._reconnect_deadline = time.monotonic() + max(0.0, (left or 5.0) - 1.5)
            log.info("Live go_away: time_left=%s", msg.go_away.time_left)

        if msg.voice_activity is not None and msg.voice_activity.voice_activity_type:
            kind = msg.voice_activity.voice_activity_type
            if kind == types.VoiceActivityType.ACTIVITY_END:
                await self._events.on_voice_activity("end")
            elif kind == types.VoiceActivityType.ACTIVITY_START:
                await self._events.on_voice_activity("start")

        sc = msg.server_content
        if sc is not None:
            await self._handle_server_content(sc)

        if msg.tool_call is not None and msg.tool_call.function_calls:
            await self._events.on_tool_call(list(msg.tool_call.function_calls))

        if msg.tool_call_cancellation is not None and msg.tool_call_cancellation.ids:
            await self._events.on_tool_cancel(list(msg.tool_call_cancellation.ids))

        if msg.usage_metadata is not None:
            await self._events.on_usage(msg.usage_metadata)

    async def _handle_server_content(self, sc: types.LiveServerContent) -> None:
        if sc.interrupted:
            self._agent_speaking = False
            await self._close_agent_transcript()
            await self._events.on_interrupted()

        if sc.input_transcription is not None:
            t = sc.input_transcription
            if t.text:
                self._in_text_open = True
                await self._events.on_transcript("user", t.text, False)
            if t.finished and self._in_text_open:
                self._in_text_open = False
                await self._events.on_transcript("user", "", True)

        if sc.model_turn is not None and sc.model_turn.parts:
            # The model has started answering: the user's utterance is over.
            if self._in_text_open:
                self._in_text_open = False
                await self._events.on_transcript("user", "", True)
            for part in sc.model_turn.parts:
                blob = part.inline_data
                if blob is not None and blob.data and (blob.mime_type or "").startswith("audio/"):
                    self._agent_speaking = True
                    await self._events.on_audio(blob.data)

        if sc.output_transcription is not None:
            t = sc.output_transcription
            if t.text:
                if self._in_text_open:
                    self._in_text_open = False
                    await self._events.on_transcript("user", "", True)
                self._out_text_open = True
                await self._events.on_transcript("agent", t.text, False)
            if t.finished:
                await self._close_agent_transcript()

        if sc.generation_complete:
            self._agent_speaking = False

        if sc.turn_complete:
            self._agent_speaking = False
            await self._close_agent_transcript()
            await self._events.on_turn_complete()

    async def _close_agent_transcript(self) -> None:
        if self._out_text_open:
            self._out_text_open = False
            await self._events.on_transcript("agent", "", True)


def _parse_duration_s(value: Any) -> float | None:
    """Parse a protobuf-style duration ("12s", "1.5s") or number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().rstrip("s")
    try:
        return float(s)
    except ValueError:
        return None
