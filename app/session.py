"""One reviewer session: client WebSocket ⇄ Gemini Live ⇄ CRM tools ⇄ browser.

The session is transport-agnostic: it talks to the client through a small
`Transport` (send_text/send_bytes), so tests can drive it with fakes.

Outgoing traffic goes through a single writer task:
- control JSON and agent audio are queued in order (never dropped, except agent
  audio that is still queued when the user barges in)
- live-view frames use a one-slot mailbox: if the socket is backed up, older
  frames are replaced by the newest one (dropped)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from array import array
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence

from google.genai import types

from app import prompts
from app.config import Settings, settings as default_settings
from app.live import LiveAgent, LiveError, build_live_config, candidate_models, make_client
from app.metrics import MetricsStore, get_store

log = logging.getLogger(__name__)

AUDIO_KIND = b"\x01"
FRAME_KIND = b"\x02"

# Mic energy threshold (RMS on int16) above which a chunk counts as speech.
SPEECH_RMS = 500
NUDGE_AFTER_S = 10.0
SUMMARY_MAX = 160

NUDGE_TEXT = (
    "[The user has been silent for about 10 seconds. Give one very short nudge "
    "(e.g. 'Still there? You can tell me the email, or skip it.') and then wait.]"
)


class Transport(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...


RunTool = Callable[[Any, str, dict[str, Any]], Awaitable[dict[str, Any]]]
LiveFactory = Callable[["Session"], Awaitable[LiveAgent]]
SlotAcquire = Callable[[], bool]
SlotRelease = Callable[[], None]


def _rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    # Sub-sample for speed; 1 in 4 samples is plenty for an energy gate.
    sub = samples[::4]
    return (sum(s * s for s in sub) / len(sub)) ** 0.5


def _short(text: Any, limit: int = SUMMARY_MAX) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


@dataclass
class TurnStats:
    """Latency bookkeeping for one user → agent exchange."""

    user_end: float | None = None  # end of user speech, or typed send
    source: str = "voice"
    first_audio: float | None = None
    tool_ms: float = 0.0
    # True while the agent is still finishing an earlier response: its audio must
    # not count as the answer to this turn until a turn boundary is seen.
    wait_boundary: bool = False


@dataclass
class Outcome:
    saves: int = 0
    corrections: int = 0
    barge_ins: int = 0
    cancels: int = 0
    turns: int = 0
    tool_errors: int = 0
    # Field values on the current form/record, to detect corrections.
    current_fields: dict[str, str] = field(default_factory=dict)

    @property
    def interventions(self) -> int:
        return self.corrections + self.cancels


class Session:
    def __init__(
        self,
        transport: Transport,
        *,
        browser: Any = None,
        run_tool: RunTool | None = None,
        tool_declarations: Sequence[dict[str, Any]] | None = None,
        live_factory: LiveFactory | None = None,
        settings: Settings = default_settings,
        metrics: MetricsStore | None = None,
        acquire_slot: SlotAcquire | None = None,
        release_slot: SlotRelease | None = None,
        nudge_after_s: float = NUDGE_AFTER_S,
        idle_timeout_s: float | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.transport = transport
        self.browser = browser
        self.settings = settings
        self.metrics = metrics or get_store()
        self._run_tool = run_tool
        self._tool_declarations = tool_declarations
        self._live_factory = live_factory or _default_live_factory
        self._acquire_slot = acquire_slot
        self._release_slot = release_slot
        self._nudge_after_s = nudge_after_s
        self._idle_timeout_s = (
            idle_timeout_s if idle_timeout_s is not None else float(settings.idle_timeout_s)
        )

        self.crm: Any = None
        self.live: LiveAgent | None = None
        self.started = False
        self._slot_held = False
        self._closed = False
        self._t0 = time.monotonic()
        self._end_reason = "disconnect"

        # Outgoing writer
        self._out: deque[tuple[str, Any]] = deque()  # ("text"|"audio", payload)
        self._frame: bytes | None = None
        self._audio_carry = b""
        self._wake = asyncio.Event()
        self._writer: asyncio.Task | None = None
        self.frames_sent = 0
        self.frames_dropped = 0

        # Tools
        self._tool_lock = asyncio.Lock()  # one shared page per session
        self._tool_tasks: set[asyncio.Task] = set()
        self._running_calls: dict[str, asyncio.Task] = {}
        self._cancelled_ids: set[str] = set()

        # Turn / activity tracking
        self._turn = TurnStats()
        self._last_voice = 0.0  # monotonic time of last speech-like mic chunk
        self._last_user_activity = time.monotonic()
        self._agent_busy = False  # agent speaking or tools running
        self._last_turn_end = 0.0  # monotonic time of the last agent turn_complete
        self._last_agent_audio = 0.0
        self._nudged = False
        self._mic_on = False
        self.outcome = Outcome()
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ client I/O

    async def handle_client_text(self, raw: str) -> bool:
        """Handle a JSON message from the client. Returns False to end the session."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_event("error", message="Invalid JSON message")
            return True
        kind = msg.get("type")
        if kind == "start":
            if not self.started:
                self.started = True
                self._spawn(self.start())
        elif kind == "text":
            text = str(msg.get("text") or "").strip()
            if text:
                await self.send_typed_text(text)
        elif kind == "mic":
            self._mic_on = bool(msg.get("on"))
            if not self._mic_on and self.live is not None:
                await self.live.end_audio_stream()
        elif kind == "stop":
            self._end_reason = "stop"
            return False
        return True

    async def handle_client_audio(self, pcm: bytes) -> None:
        now = time.monotonic()
        if _rms(pcm) >= SPEECH_RMS:
            self._last_voice = now
            self._mark_user_activity()
        if self.live is not None:
            await self.live.send_audio(pcm)

    async def send_typed_text(self, text: str) -> None:
        if self.live is None:
            await self.send_event("error", message="Session is not ready yet. Press Start first.")
            return
        self._mark_user_activity()
        now = time.monotonic()
        speaking = now - self._last_agent_audio < 0.3
        self._turn = TurnStats(user_end=now, source="text", wait_boundary=speaking)
        # The client renders typed messages itself; do not echo them back.
        try:
            await self.live.send_text(text)
        except Exception as exc:
            log.warning("[%s] send_text failed: %s", self.id, exc)
            await self.send_event("error", message="Could not send the message to the agent.")

    # ------------------------------------------------------------------ outgoing

    async def send_event(self, type_: str, **fields: Any) -> None:
        self._enqueue("text", json.dumps({"type": type_, **fields}, default=str))

    def _enqueue(self, kind: str, payload: Any) -> None:
        if self._closed:
            return
        self._out.append((kind, payload))
        self._wake.set()

    async def on_frame(self, jpeg: bytes) -> None:
        """Called by the browser screencast. Keeps only the newest frame."""
        if self._closed:
            return
        if self._frame is not None:
            self.frames_dropped += 1
        self._frame = jpeg
        self._wake.set()

    async def _writer_loop(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._out or self._frame is not None:
                    if self._out:
                        kind, payload = self._out.popleft()
                        if kind == "text":
                            await self.transport.send_text(payload)
                        else:
                            await self.transport.send_bytes(AUDIO_KIND + payload)
                    else:
                        frame, self._frame = self._frame, None
                        if frame is not None:
                            await self.transport.send_bytes(FRAME_KIND + frame)
                            self.frames_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # client went away
            log.info("[%s] writer stopped: %s", self.id, exc)

    async def flush(self, timeout: float = 1.0) -> None:
        """Wait until queued control/audio messages are written (best effort)."""
        deadline = time.monotonic() + timeout
        while self._out and time.monotonic() < deadline:
            if self._writer is None or self._writer.done():
                return
            await asyncio.sleep(0.01)

    def start_writer(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._writer_loop(), name=f"writer-{self.id}")

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.start_writer()
        if self._acquire_slot is not None:
            if not self._acquire_slot():
                await self.send_event(
                    "status",
                    state="busy",
                    message="All demo seats are taken right now. Please try again in a minute.",
                )
                self._end_reason = "busy"
                await self.flush()
                await self.shutdown(reason="busy", notify=False)
                return
            self._slot_held = True

        if not self.settings.has_api_key:
            msg = "The server has no GEMINI_API_KEY configured, so the voice agent cannot start."
            await self.send_event("error", message=msg)
            await self.send_event("status", state="ended", message=msg)
            await self.shutdown(reason="no_api_key", notify=False)
            return

        await self.send_event(
            "status", state="starting", message="Opening the CRM and connecting the voice agent…"
        )
        try:
            if self._run_tool is None or self._tool_declarations is None:
                from app.crm.tools import TOOL_DECLARATIONS, run_tool

                self._run_tool = self._run_tool or run_tool
                if self._tool_declarations is None:
                    self._tool_declarations = TOOL_DECLARATIONS
            if self.browser is None:
                raise RuntimeError("Browser is not available on the server")

            crm_task = asyncio.create_task(self.browser.new_session(on_frame=self.on_frame))
            live_task = asyncio.create_task(self._live_factory(self))
            try:
                self.crm, self.live = await asyncio.gather(crm_task, live_task)
            except BaseException:
                for t in (crm_task, live_task):
                    t.cancel()
                for t in (crm_task, live_task):
                    if t.done() and not t.cancelled() and t.exception() is None:
                        res = t.result()
                        with contextlib.suppress(Exception):
                            await res.close()
                raise
        except LiveError as exc:
            log.warning("[%s] live start failed: %s", self.id, exc)
            await self._fail(_friendly_live_error(exc))
            return
        except Exception as exc:
            log.exception("[%s] session start failed", self.id)
            await self._fail(f"Could not start the session: {_short(exc, 200)}")
            return

        self.metrics.record("session_start", session_id=self.id, model=self.live.model)
        self._mark_user_activity()
        self._spawn(self._watch_live())
        self._spawn(self._idle_watchdog())
        await self.send_event("status", state="ready", message=f"Connected ({self.live.model})")

    async def _fail(self, message: str) -> None:
        await self.send_event("error", message=message)
        await self.send_event("status", state="ended", message=message)
        await self.shutdown(reason="start_failed", notify=False)

    async def _watch_live(self) -> None:
        """End the session if the Live connection dies for good."""
        assert self.live is not None
        task = self.live.done_task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await task
            except Exception as exc:
                log.warning("[%s] live connection ended: %s", self.id, exc)
                if not self._closed:
                    await self.send_event("error", message=_friendly_live_error(exc))
                    await self.shutdown(reason="live_error")
                return
            if not self._closed and not self.live.closed:
                await self.shutdown(reason="live_closed")

    async def _idle_watchdog(self) -> None:
        hard_cap = self.settings.session_max_minutes * 60
        try:
            while not self._closed:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                idle = now - self._last_user_activity
                quiet = now - max(self._last_user_activity, self._last_turn_end)
                if now - self._t0 > hard_cap:
                    await self.send_event("status", state="ended", message="Session time limit reached.")
                    await self.shutdown(reason="max_duration", notify=False)
                    return
                if idle > self._idle_timeout_s:
                    await self.send_event(
                        "status", state="ended", message="Session ended after a period of silence."
                    )
                    await self.shutdown(reason="idle", notify=False)
                    return
                if (
                    not self._nudged
                    and not self._agent_busy
                    and not self._tool_tasks
                    and quiet > self._nudge_after_s
                    and self.live is not None
                ):
                    self._nudged = True
                    with contextlib.suppress(Exception):
                        await self.live.send_text(NUDGE_TEXT)
        except asyncio.CancelledError:
            pass

    def _mark_user_activity(self) -> None:
        self._last_user_activity = time.monotonic()
        self._nudged = False

    async def shutdown(self, reason: str | None = None, notify: bool = True) -> None:
        if self._closed:
            return
        if reason:
            self._end_reason = reason
        if notify:
            await self.send_event("status", state="ended", message="Session ended.")
        # Mark closed and free the seat before any await, so a cancelled shutdown
        # (e.g. server stopping) can never leak a session slot.
        self._closed = True
        if self._slot_held and self._release_slot is not None:
            self._slot_held = False
            self._release_slot()
        await self.flush(0.5)

        current = asyncio.current_task()
        for t in list(self._tool_tasks) + list(self._bg_tasks):
            if t is not current:
                t.cancel()
        if self.live is not None:
            with contextlib.suppress(Exception):
                await self.live.close()
        if self.crm is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.crm.close(), 10)
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer
        close_transport = getattr(self.transport, "close", None)
        if callable(close_transport):
            with contextlib.suppress(Exception):
                await close_transport()

        if self.live is not None:  # only sessions that really started
            o = self.outcome
            self.metrics.record(
                "outcome",
                session_id=self.id,
                completed=o.saves > 0,
                saves=o.saves,
                corrections=o.corrections,
                interventions=o.interventions,
                barge_ins=o.barge_ins,
                cancels=o.cancels,
                turns=o.turns,
                tool_errors=o.tool_errors,
            )
            self.metrics.record(
                "session_end",
                session_id=self.id,
                duration_s=round(time.monotonic() - self._t0, 1),
                reason=self._end_reason,
                frames_sent=self.frames_sent,
                frames_dropped=self.frames_dropped,
            )
        log.info("[%s] session closed (%s)", self.id, self._end_reason)

    @property
    def closed(self) -> bool:
        return self._closed

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # ------------------------------------------------------------------ LiveEvents

    async def on_audio(self, pcm: bytes) -> None:
        self._agent_busy = True
        t = self._turn
        now = time.monotonic()
        self._last_agent_audio = now
        if t.first_audio is None and not t.wait_boundary:
            if t.user_end is None and self._last_voice > self._last_turn_end:
                # No VAD end reported: the user stopped at the last voiced mic chunk.
                t.user_end = self._last_voice
            if t.user_end is not None:
                t.first_audio = now
        # Keep PCM16 frames even-length for the client (carry a stray byte).
        if self._audio_carry:
            pcm = self._audio_carry + pcm
            self._audio_carry = b""
        if len(pcm) % 2:
            pcm, self._audio_carry = pcm[:-1], pcm[-1:]
        if pcm:
            self._enqueue("audio", pcm)

    async def on_transcript(self, role: str, text: str, final: bool) -> None:
        if role == "user":
            self._mark_user_activity()
            # A new spoken user turn begins (unless one is already being tracked).
            if self._turn.first_audio is not None or self._turn.source != "voice":
                self._turn = TurnStats(source="voice")
        await self.send_event("transcript", role=role, text=text, final=final)

    async def on_voice_activity(self, kind: str) -> None:
        if kind == "end":
            self._turn.user_end = time.monotonic()
        elif kind == "start":
            self._mark_user_activity()

    async def on_interrupted(self) -> None:
        self.outcome.barge_ins += 1
        # Drop agent audio that has not been written to the socket yet.
        self._out = deque(item for item in self._out if item[0] != "audio")
        self._audio_carry = b""
        self._agent_busy = False
        self._turn.wait_boundary = False
        await self.send_event("interrupted")

    async def on_turn_complete(self) -> None:
        self._agent_busy = bool(self._tool_tasks)
        self._last_turn_end = time.monotonic()
        await self.send_event("turn_complete")
        t = self._turn
        if t.wait_boundary:
            t.wait_boundary = False  # the earlier response ended; now measure this turn
            return
        if t.first_audio is None or t.user_end is None:
            return  # e.g. a tool-only step, or no measurable user turn
        start = t.user_end
        first_ms = max(0.0, (t.first_audio - start) * 1000)
        turn_ms = max(0.0, (time.monotonic() - start) * 1000)
        self.outcome.turns += 1
        await self.send_event(
            "metrics",
            first_audio_ms=round(first_ms),
            tool_ms=round(t.tool_ms),
            turn_ms=round(turn_ms),
        )
        self.metrics.record(
            "turn",
            session_id=self.id,
            source=t.source,
            first_audio_ms=round(first_ms, 1),
            turn_ms=round(turn_ms, 1),
            tool_ms=round(t.tool_ms, 1),
        )
        self._turn = TurnStats(source="voice")

    async def on_usage(self, usage: types.UsageMetadata) -> None:
        log.debug("[%s] usage: total=%s", self.id, usage.total_token_count)

    async def on_status(self, state: str, message: str) -> None:
        await self.send_event("status", state=state, message=message)

    async def on_tool_call(self, calls: list[types.FunctionCall]) -> None:
        self._agent_busy = True
        task = asyncio.create_task(self._run_calls(calls), name=f"tools-{self.id}")
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def on_tool_cancel(self, ids: list[str]) -> None:
        for call_id in ids:
            self._cancelled_ids.add(call_id)
            running = self._running_calls.get(call_id)
            if running is not None and not running.done():
                running.cancel()

    # ------------------------------------------------------------------ tools

    async def _run_calls(self, calls: list[types.FunctionCall]) -> None:
        """Run one tool_call message's calls in order and answer them together."""
        assert self.live is not None
        live = self.live
        live.tool_started()
        responses: list[types.FunctionResponse] = []
        try:
            async with self._tool_lock:
                for call in calls:
                    call_id = call.id or uuid.uuid4().hex[:8]
                    name = call.name or ""
                    args = dict(call.args or {})
                    if call_id in self._cancelled_ids:
                        await self._emit_action(call_id, name, args, "error", "Cancelled", 0)
                        continue
                    result = await self._run_one(call_id, name, args)
                    if result is None:  # cancelled while running
                        continue
                    responses.append(self._function_response(call.id, name, result))
            if responses and not self._closed:
                await live.send_tool_responses(responses)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] tool batch failed", self.id)
        finally:
            live.tool_finished()

    def _function_response(
        self, call_id: str | None, name: str, result: dict[str, Any]
    ) -> types.FunctionResponse:
        if self.settings.live_async_tools:
            # NON_BLOCKING tools: deliver the result when the model is done talking.
            return types.FunctionResponse(
                id=call_id,
                name=name,
                response=result,
                scheduling=types.FunctionResponseScheduling.WHEN_IDLE,
            )
        return types.FunctionResponse(id=call_id, name=name, response=result)

    async def _run_one(self, call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        await self._emit_action(call_id, name, args, "running", "", None)
        t0 = time.monotonic()
        inner = asyncio.create_task(self._invoke_tool(name, args))
        self._running_calls[call_id] = inner
        cancelled = False
        try:
            result = await inner
        except asyncio.CancelledError:
            if not inner.cancelled():
                raise  # the batch itself was cancelled (shutdown)
            cancelled = True
            result = {"ok": False, "observation": "Cancelled."}
        finally:
            self._running_calls.pop(call_id, None)
        ms = (time.monotonic() - t0) * 1000
        ok = bool(result.get("ok"))
        self._turn.tool_ms += ms
        if not ok:
            self.outcome.tool_errors += 1
        self.metrics.record("tool", session_id=self.id, name=name, ms=round(ms, 1), ok=ok)
        summary = "Cancelled" if cancelled else _short(result.get("observation") or result.get("error"))
        await self._emit_action(call_id, name, args, "ok" if ok else "error", summary, round(ms))
        if cancelled:
            return None
        self._track_outcome(name, args, result)
        await self._emit_form(result)
        log.info("[%s] tool %s ok=%s %.0fms args=%s obs=%s", self.id, name, ok, ms, args, summary)
        return result

    async def _invoke_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self._run_tool is not None
        try:
            raw = await asyncio.wait_for(
                self._run_tool(self.crm, name, args), timeout=self.settings.tool_timeout_s
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "observation": f"The action timed out after {self.settings.tool_timeout_s}s. "
                "Call get_screen to check the current state before retrying.",
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[%s] tool %s raised", self.id, name)
            return {"ok": False, "observation": f"Tool error: {_short(exc, 200)}"}
        if not isinstance(raw, dict):
            raw = {"ok": bool(raw), "observation": str(raw)}
        raw.setdefault("ok", False)
        raw.setdefault("observation", "")
        return _jsonable(raw)

    async def _emit_action(
        self,
        call_id: str,
        name: str,
        args: dict[str, Any],
        status: str,
        summary: str,
        ms: float | None,
    ) -> None:
        await self.send_event(
            "action", id=call_id, name=name, args=args, status=status, summary=summary, ms=ms
        )

    async def _emit_form(self, result: dict[str, Any]) -> None:
        form = result.get("form")
        if isinstance(form, dict) and form:
            await self.send_event(
                "form",
                entity=form.get("entity"),
                fields=form.get("fields") or {},
                url=form.get("url"),
            )
        elif isinstance(result.get("fields"), dict) and result.get("entity"):
            await self.send_event(
                "form", entity=result.get("entity"), fields=result["fields"], url=result.get("url")
            )

    def _track_outcome(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        o = self.outcome
        ok = bool(result.get("ok"))
        if name in {"open_create_form", "open_record"} and ok:
            o.current_fields = {}
        elif name == "fill_fields":
            fields = args.get("fields") if isinstance(args.get("fields"), dict) else args
            for key, value in (fields or {}).items():
                new = str(value).strip().lower()
                old = o.current_fields.get(key)
                if old is not None and old != new:
                    o.corrections += 1
                o.current_fields[key] = new
        elif name == "set_stage":
            stage = str(args.get("stage", "")).strip().lower()
            old = o.current_fields.get("stage")
            if old is not None and old != stage:
                o.corrections += 1
            o.current_fields["stage"] = stage
            if ok:
                o.saves += 1
        elif name == "save" and ok:
            o.saves += 1
        elif name == "cancel" and ok:
            o.cancels += 1
            o.current_fields = {}


def _friendly_live_error(exc: BaseException) -> str:
    text = str(exc)
    low = text.lower()
    if "429" in low or "quota" in low or "resource_exhausted" in low or "rate" in low:
        return "The voice service is at its free-tier limit. Please retry in a minute."
    if "api key" in low or "permission" in low or "401" in low or "403" in low:
        return "The voice service rejected the server's API key."
    return f"The voice service is unavailable right now ({_short(text, 120)})."


async def _default_live_factory(session: Session) -> LiveAgent:
    s = session.settings
    client = make_client(s.gemini_api_key)
    models = await candidate_models(client, s.gemini_live_model)
    system_instruction = prompts.build_system_instruction()
    declarations = list(session._tool_declarations or [])

    def config_factory(handle: str | None) -> types.LiveConnectConfig:
        return build_live_config(
            system_instruction=system_instruction,
            tool_declarations=declarations,
            voice=s.live_voice,
            start_sensitivity=s.vad_start_sensitivity,
            end_sensitivity=s.vad_end_sensitivity,
            prefix_padding_ms=s.vad_prefix_padding_ms,
            silence_duration_ms=s.vad_silence_duration_ms,
            non_blocking_tools=s.live_async_tools,
            resumption_handle=handle,
        )

    agent = LiveAgent(
        connect=client.aio.live.connect,
        models=models,
        config_factory=config_factory,
        events=session,
        kickoff_text=prompts.GREETING_KICKOFF,
        text_mode=s.live_text_mode,
    )
    await agent.start()
    return agent
