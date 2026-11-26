"""Session event flow with a fake Live API, fake browser and fake client socket."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from app import prompts, session as session_mod
from app.config import settings
from app.live import LiveAgent, build_live_config
from app.metrics import MetricsStore
from app.session import AUDIO_KIND, FRAME_KIND, Session
from tests.fakes import (
    FakeBrowser,
    FakeConnector,
    FakeTransport,
    audio_msg,
    cancel_msg,
    go_away_msg,
    in_text_msg,
    interrupted_msg,
    out_text_msg,
    resumption_msg,
    tool_call_msg,
    turn_complete_msg,
    wait_until,
)

pytestmark = pytest.mark.asyncio

DECLS = [
    {"name": "open_create_form", "description": "Open a create form.",
     "parameters": {"type": "OBJECT", "properties": {"entity": {"type": "STRING"}}}},
    {"name": "fill_fields", "description": "Fill fields.",
     "parameters": {"type": "OBJECT", "properties": {"fields": {
         "type": "ARRAY", "items": {"type": "OBJECT", "properties": {
             "field": {"type": "STRING"}, "value": {"type": "STRING"}}}}}}},
    {"name": "save", "description": "Save.", "parameters": {"type": "OBJECT", "properties": {}}},
]


class ToolRecorder:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[tuple[object, str, dict]] = []
        self.delay = delay
        self.release: asyncio.Event | None = None

    async def __call__(self, crm, name, args):
        self.calls.append((crm, name, args))
        if self.release is not None:
            await self.release.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if name == "save":
            return {"ok": True, "observation": "Saved. Detail view: Maya Okafor.",
                    "form": {"entity": "Contact", "fields": {"Name": "Maya Okafor"}, "url": "u"}}
        return {"ok": True, "observation": f"{name} done"}


def make_session(tmp_path, *, connector=None, tools=None, browser=None, model="gemini-3.8-live",
                 api_key="test-key", acquire=None, release=None, stall_timeout_s=None, **kw):
    connector = connector or FakeConnector()
    tools = tools or ToolRecorder()
    browser = browser or FakeBrowser(frames=[b"f0"])
    s = dataclasses.replace(settings, gemini_api_key=api_key, live_text_mode="auto")
    transport = FakeTransport()

    async def live_factory(session: Session) -> LiveAgent:
        agent = LiveAgent(
            connect=connector.connect,
            models=[m for m in ([model] if isinstance(model, str) else model)],
            config_factory=lambda handle: build_live_config(
                system_instruction="test", tool_declarations=DECLS, resumption_handle=handle
            ),
            events=session,
            stall_timeout_s=stall_timeout_s,
        )
        await agent.start(timeout=2)
        return agent

    session = Session(
        transport,
        browser=browser,
        run_tool=tools,
        tool_declarations=DECLS,
        live_factory=live_factory,
        settings=s,
        metrics=MetricsStore(tmp_path / "m.jsonl"),
        acquire_slot=acquire,
        release_slot=release,
        **kw,
    )
    return session, transport, connector, tools, browser


async def start(session: Session, transport: FakeTransport):
    session.start_writer()
    assert await session.handle_client_text(json.dumps({"type": "start"}))
    await wait_until(lambda: any(e.get("state") == "ready" for e in transport.events("status")))
    # The greeting kickoff follows "ready".
    live = session.live._session
    await wait_until(lambda: live.texts or live.client_contents)


async def test_start_greets_forwards_frames_and_pumps_mic(tmp_path):
    session, transport, connector, _, browser = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]

    # Kickoff greeting goes out as realtime text for 3.x models.
    assert live.texts == [prompts.GREETING_KICKOFF]
    # The first screencast frame reached the client as 0x02 + jpeg.
    await wait_until(lambda: FRAME_KIND + b"f0" in transport.binary)

    # Mic audio is forwarded as 16 kHz PCM blobs.
    await session.handle_client_audio(b"\x00\x01" * 160)
    assert live.audio[-1].mime_type == "audio/pcm;rate=16000"
    assert live.audio[-1].data == b"\x00\x01" * 160

    # Mic off → audio_stream_end
    await session.handle_client_text(json.dumps({"type": "mic", "on": False}))
    await wait_until(lambda: live.audio_stream_ends == 1)

    await session.shutdown()
    assert browser.crm.closed and session.closed


async def test_agent_audio_transcripts_and_turn_metrics(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]

    await session.send_typed_text("create a contact")
    assert live.texts[-1] == "create a contact"
    # Typed text is not echoed back as a user transcript.
    assert not [e for e in transport.events("transcript") if e["role"] == "user"]

    live.push(out_text_msg("Opening "))
    live.push(audio_msg(b"\x01\x02\x03\x04"))
    live.push(out_text_msg("the form.", finished=True))
    live.push(turn_complete_msg())
    await wait_until(lambda: transport.events("metrics"))

    assert AUDIO_KIND + b"\x01\x02\x03\x04" in transport.binary
    agent = [e for e in transport.events("transcript") if e["role"] == "agent"]
    assert [e["text"] for e in agent if not e["final"]] == ["Opening ", "the form."]
    assert agent[-1]["final"] is True
    assert transport.events("turn_complete")
    m = transport.events("metrics")[0]
    assert m["first_audio_ms"] >= 0 and m["turn_ms"] >= m["first_audio_ms"]
    await session.shutdown()


async def test_user_transcript_closed_when_agent_answers(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]
    live.push(in_text_msg("hello "))
    live.push(in_text_msg("there"))
    live.push(audio_msg(b"\x00\x00"))
    await wait_until(lambda: any(e["role"] == "user" and e["final"] for e in transport.events("transcript")))
    user = [e for e in transport.events("transcript") if e["role"] == "user"]
    assert [e["text"] for e in user] == ["hello ", "there", ""]
    await session.shutdown()


async def test_tool_call_runs_tool_and_sends_response(tmp_path):
    session, transport, connector, tools, browser = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]

    live.push(tool_call_msg(
        ("c1", "open_create_form", {"entity": "Contact"}),
        ("c2", "fill_fields", {"fields": [{"field": "lastName", "value": "Smith"}]}),
    ))
    await wait_until(lambda: live.tool_responses)

    # Executed in order, against the session's CRM page.
    assert [(c[1], c[2]) for c in tools.calls] == [
        ("open_create_form", {"entity": "Contact"}),
        ("fill_fields", {"fields": [{"field": "lastName", "value": "Smith"}]}),
    ]
    assert all(c[0] is browser.crm for c in tools.calls)
    # One tool response message answering both calls, ids preserved.
    responses = live.tool_responses[0]
    assert [(r.id, r.name) for r in responses] == [("c1", "open_create_form"), ("c2", "fill_fields")]
    assert responses[0].response == {"ok": True, "observation": "open_create_form done"}

    await wait_until(lambda: len(transport.events("action")) == 4)
    actions = transport.events("action")
    assert [(a["id"], a["status"]) for a in actions] == [
        ("c1", "running"), ("c1", "ok"), ("c2", "running"), ("c2", "ok")]
    assert actions[1]["ms"] is not None and actions[1]["summary"] == "open_create_form done"
    await session.shutdown()


async def test_correction_and_save_tracked_in_outcome(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]
    live.push(tool_call_msg(("a", "open_create_form", {"entity": "Contact"})))
    live.push(tool_call_msg(("b", "fill_fields", {"fields": {"lastName": "Smith"}})))
    live.push(tool_call_msg(("c", "fill_fields", {"fields": {"lastName": "Smyth"}})))
    live.push(tool_call_msg(("d", "save", {})))
    await wait_until(lambda: len(live.tool_responses) == 4)
    assert session.outcome.corrections == 1
    assert session.outcome.saves == 1
    form = transport.events("form")
    assert form and form[0]["entity"] == "Contact" and form[0]["fields"] == {"Name": "Maya Okafor"}
    await session.shutdown()

    events = session.metrics.events()
    outcome = [e for e in events if e["kind"] == "outcome"][0]
    assert outcome["completed"] is True and outcome["corrections"] == 1
    assert len([e for e in events if e["kind"] == "tool"]) == 4


async def test_tool_errors_and_exceptions_become_observations(tmp_path):
    async def boom(crm, name, args):
        raise RuntimeError("selector not found")

    session, transport, connector, _, _ = make_session(tmp_path, tools=boom)
    await start(session, transport)
    live = connector.sessions[0]
    live.push(tool_call_msg(("x", "save", {})))
    await wait_until(lambda: live.tool_responses)
    resp = live.tool_responses[0][0].response
    assert resp["ok"] is False and "selector not found" in resp["observation"]
    assert transport.events("action")[-1]["status"] == "error"
    await session.shutdown()


async def test_interrupted_is_forwarded_and_queued_audio_dropped(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]

    transport.gate = asyncio.Event()  # back up the socket
    live.push(audio_msg(b"\xaa\xaa"))
    live.push(audio_msg(b"\xbb\xbb"))
    live.push(interrupted_msg())
    await wait_until(lambda: session.outcome.barge_ins == 1)
    transport.gate.set()
    await wait_until(lambda: transport.events("interrupted"))

    sent_audio = [b for b in transport.binary if b[:1] == AUDIO_KIND]
    # At most the chunk already in flight was written; queued chunks were dropped.
    assert AUDIO_KIND + b"\xbb\xbb" not in sent_audio
    await session.shutdown()


async def test_tool_call_cancellation_cancels_running_tool(tmp_path):
    tools = ToolRecorder()
    tools.release = asyncio.Event()  # tool blocks until released
    session, transport, connector, _, _ = make_session(tmp_path, tools=tools)
    await start(session, transport)
    live = connector.sessions[0]

    live.push(tool_call_msg(("slow", "open_create_form", {"entity": "Lead"})))
    await wait_until(lambda: tools.calls)
    live.push(cancel_msg("slow"))
    await wait_until(lambda: any(a["status"] == "error" for a in transport.events("action")))
    await asyncio.sleep(0.05)
    assert live.tool_responses == []  # cancelled calls are not answered
    assert transport.events("action")[-1]["summary"] == "Cancelled"
    await session.shutdown()


async def test_frames_are_dropped_when_socket_backed_up(tmp_path):
    session, transport, connector, _, browser = make_session(tmp_path, browser=FakeBrowser())
    await start(session, transport)
    await asyncio.sleep(0.02)
    transport.gate = asyncio.Event()
    for i in range(10):
        await browser.on_frame(f"frame{i}".encode())
    transport.gate.set()
    await wait_until(lambda: FRAME_KIND + b"frame9" in transport.binary)
    frames = [b for b in transport.binary if b[:1] == FRAME_KIND]
    assert len(frames) <= 3  # newest wins, older ones replaced
    assert session.frames_dropped >= 7
    await session.shutdown()


async def test_odd_length_agent_audio_kept_even(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]
    live.push(audio_msg(b"\x01\x02\x03"))
    live.push(audio_msg(b"\x04"))
    await wait_until(lambda: len([b for b in transport.binary if b[:1] == AUDIO_KIND]) == 2)
    chunks = [b[1:] for b in transport.binary if b[:1] == AUDIO_KIND]
    assert chunks == [b"\x01\x02", b"\x03\x04"]
    await session.shutdown()


async def test_go_away_reconnects_with_resumption_handle(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    first = connector.sessions[0]
    first.push(resumption_msg("handle-1"))
    first.push(go_away_msg("30s"))
    await wait_until(lambda: len(connector.sessions) == 2)
    assert connector.configs[0].session_resumption.handle is None
    assert connector.configs[1].session_resumption.handle == "handle-1"
    # Kickoff is not repeated on resume, and the new session is used for sending.
    second = connector.sessions[1]
    await wait_until(lambda: session.live._session is second)
    await session.send_typed_text("hi again")
    assert second.texts == ["hi again"]
    await session.shutdown()


async def test_dropped_connection_resumes(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    first = connector.sessions[0]
    first.push(resumption_msg("h2"))
    first.drop_connection(1011)
    await wait_until(lambda: len(connector.sessions) == 2, timeout=3)
    assert connector.configs[1].session_resumption.handle == "h2"
    assert not session.closed
    await session.shutdown()


async def test_dropped_connection_without_handle_ends_session(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    connector.sessions[0].drop_connection(1011)
    await wait_until(lambda: transport.closed)
    assert transport.events("error")


async def test_falls_back_to_next_model(tmp_path):
    connector = FakeConnector(fail_models={"bad-model"})
    session, transport, _, _, _ = make_session(
        tmp_path, connector=connector, model=["bad-model", "gemini-2.5-flash-native-audio-latest"])
    await start(session, transport)
    assert connector.models == ["bad-model", "gemini-2.5-flash-native-audio-latest"]
    assert session.live.model == "gemini-2.5-flash-native-audio-latest"
    # 2.5 models get typed text via client_content.
    assert connector.sessions[0].client_contents and not connector.sessions[0].texts
    await session.shutdown()


async def test_missing_api_key_reports_error(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path, api_key="")
    session.start_writer()
    await session.handle_client_text(json.dumps({"type": "start"}))
    await wait_until(lambda: transport.closed)
    assert "GEMINI_API_KEY" in transport.events("error")[0]["message"]
    assert transport.events("status")[-1]["state"] == "ended"
    assert connector.sessions == []


async def test_busy_when_no_slot(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path, acquire=lambda: False)
    session.start_writer()
    await session.handle_client_text(json.dumps({"type": "start"}))
    await wait_until(lambda: transport.closed)
    assert transport.events("status")[-1]["state"] == "busy"
    assert connector.sessions == []


async def test_slot_released_on_shutdown(tmp_path):
    held = {"n": 0}

    def acquire():
        held["n"] += 1
        return True

    def release():
        held["n"] -= 1

    session, transport, connector, _, _ = make_session(tmp_path, acquire=acquire, release=release)
    await start(session, transport)
    assert held["n"] == 1
    assert await session.handle_client_text(json.dumps({"type": "stop"})) is False
    await session.shutdown()
    assert held["n"] == 0
    assert connector.sessions[0].closed
    kinds = [e["kind"] for e in session.metrics.events()]
    assert kinds[0] == "session_start" and "outcome" in kinds and kinds[-1] == "session_end"


async def test_idle_timeout_ends_session(tmp_path):
    session, transport, connector, _, _ = make_session(
        tmp_path, idle_timeout_s=0.5, nudge_after_s=100)
    await start(session, transport)
    await wait_until(lambda: transport.closed, timeout=4)
    assert transport.events("status")[-1]["state"] == "ended"


async def test_silence_nudge_sent_once(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path, nudge_after_s=0.2)
    await start(session, transport)
    live = connector.sessions[0]
    await wait_until(lambda: len(live.texts) >= 2, timeout=4)
    await asyncio.sleep(1.3)
    nudges = [t for t in live.texts if "silent" in t]
    assert len(nudges) == 1
    await session.shutdown()


async def test_text_sent_while_agent_speaking_waits_for_turn_boundary(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]
    live.push(audio_msg(b"\x00\x00"))  # agent is talking (no user turn yet)
    await wait_until(lambda: any(b[:1] == AUDIO_KIND for b in transport.binary))
    await session.send_typed_text("next")
    live.push(audio_msg(b"\x00\x00"))  # tail of the earlier response
    live.push(turn_complete_msg())
    await wait_until(lambda: transport.events("turn_complete"))
    assert transport.events("metrics") == []  # leftover audio is not this turn's answer
    await asyncio.sleep(0.05)
    live.push(audio_msg(b"\x01\x00"))
    live.push(turn_complete_msg())
    await wait_until(lambda: transport.events("metrics"))
    assert transport.events("metrics")[0]["first_audio_ms"] >= 40
    await session.shutdown()


class SlowBrowser(FakeBrowser):
    """CRM login that only finishes when the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def new_session(self, on_frame):
        await self.gate.wait()
        return await super().new_session(on_frame)


async def test_greeting_and_tools_wait_until_crm_is_ready(tmp_path):
    browser = SlowBrowser()
    session, transport, connector, tools, _ = make_session(tmp_path, browser=browser)
    session.start_writer()
    await session.handle_client_text(json.dumps({"type": "start"}))
    await wait_until(lambda: connector.sessions)  # Live is up, the CRM is still logging in
    live = connector.sessions[0]
    await asyncio.sleep(0.05)
    assert live.texts == []  # no greeting before the CRM is on screen

    live.push(tool_call_msg(("early", "open_create_form", {"entity": "Contact"})))
    await asyncio.sleep(0.05)
    assert tools.calls == []  # waits instead of crashing on a missing CRM page

    browser.gate.set()
    await wait_until(lambda: live.tool_responses)
    assert tools.calls[0][0] is browser.crm
    assert live.tool_responses[0][0].id == "early"
    assert live.texts == [prompts.GREETING_KICKOFF]
    await session.shutdown()


async def test_unexpected_batch_error_still_answers_every_call(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]

    def broken(*_args):
        raise RuntimeError("bug in bookkeeping")

    session._track_outcome = broken
    live.push(tool_call_msg(("a", "get_screen", {}), ("b", "save", {})))
    await wait_until(lambda: live.tool_responses)
    responses = live.tool_responses[0]
    assert [r.id for r in responses] == ["a", "b"]
    assert all(r.response["ok"] is False for r in responses)
    await session.shutdown()


async def test_corrections_counted_per_field_with_list_args(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]
    live.push(tool_call_msg(("a", "open_create_form", {"entity": "Contact"})))
    live.push(tool_call_msg(("b", "fill_fields", {"fields": [
        {"field": "firstName", "value": "Maya"}, {"field": "lastName", "value": "Smith"}]})))
    live.push(tool_call_msg(("c", "fill_fields", {"fields": [
        {"field": "emailAddress", "value": "maya@example.com"}]})))
    await wait_until(lambda: len(live.tool_responses) == 3)
    assert session.outcome.corrections == 0  # new fields are not corrections

    live.push(tool_call_msg(("d", "fill_fields", {"fields": [
        {"field": "Last Name", "value": "Smyth"}]})))  # label form of lastName
    await wait_until(lambda: len(live.tool_responses) == 4)
    assert session.outcome.corrections == 1
    await session.shutdown()


async def test_cancelled_save_finishes_and_model_is_told(tmp_path):
    tools = ToolRecorder()
    tools.release = asyncio.Event()
    session, transport, connector, _, _ = make_session(tmp_path, tools=tools)
    await start(session, transport)
    live = connector.sessions[0]

    live.push(tool_call_msg(("s1", "save", {})))
    await wait_until(lambda: tools.calls)
    live.push(cancel_msg("s1"))  # user barged in while saving
    await asyncio.sleep(0.05)
    tools.release.set()
    await wait_until(lambda: any("did finish" in t for t in live.texts))

    assert live.tool_responses == []  # the cancelled id is not answered
    final = [a for a in transport.events("action") if a["id"] == "s1"][-1]
    assert final["status"] == "ok" and "Saved" in final["summary"]
    assert session.outcome.saves == 1
    assert "Maya Okafor" in [t for t in live.texts if "did finish" in t][0]
    await session.shutdown()


async def test_long_typed_text_is_rejected(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]
    text = "x" * (session_mod.MAX_TEXT_CHARS + 1)
    await session.handle_client_text(json.dumps({"type": "text", "text": text}))
    await wait_until(lambda: transport.events("error"))
    assert "too long" in transport.events("error")[0]["message"]
    assert text not in live.texts
    await session.shutdown()


async def test_typed_text_does_not_block_receive_loop(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path)
    await start(session, transport)
    live = connector.sessions[0]
    session.live._ready.clear()  # as during a reconnect: sends would wait
    await asyncio.wait_for(
        session.handle_client_text(json.dumps({"type": "text", "text": "hello"})), 0.2)
    await asyncio.wait_for(
        session.handle_client_text(json.dumps({"type": "mic", "on": False})), 0.2)
    session.live._ready.set()
    await wait_until(lambda: "hello" in live.texts and live.audio_stream_ends == 1)
    await session.shutdown()


# ----------------------------------------------------------------- stall watchdog


async def test_stall_reconnects_with_handle_and_asks_for_a_reply(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path, stall_timeout_s=0.3)
    await start(session, transport)
    first = connector.sessions[0]
    first.push(resumption_msg("h1"))
    # The greeting got no reply at all: the service stalled.
    await wait_until(lambda: len(connector.sessions) == 2, timeout=3)
    assert connector.configs[1].session_resumption.handle == "h1"
    second = connector.sessions[1]
    await wait_until(lambda: second.texts)
    assert second.texts == [session_mod.RESUMED_RECOVERY_TEXT]
    assert any(e["state"] == "reconnecting" for e in transport.events("status"))
    # The model answers now: no further reconnects.
    second.push(audio_msg(b"\x00\x00"))
    second.push(turn_complete_msg())
    await asyncio.sleep(0.6)
    assert len(connector.sessions) == 2 and not session.closed
    stalls = [e for e in session.metrics.events() if e["kind"] == "stall"]
    assert len(stalls) == 1 and stalls[0]["waited_ms"] >= 300
    await session.shutdown()


async def test_no_stall_while_a_tool_is_running(tmp_path):
    tools = ToolRecorder()
    tools.release = asyncio.Event()
    session, transport, connector, _, _ = make_session(tmp_path, tools=tools, stall_timeout_s=0.2)
    await start(session, transport)
    live = connector.sessions[0]
    live.push(tool_call_msg(("slow", "get_screen", {})))  # the model replies with a tool call
    await wait_until(lambda: tools.calls)
    await asyncio.sleep(0.6)  # tool runs longer than the stall timeout
    assert len(connector.sessions) == 1
    tools.release.set()
    await wait_until(lambda: live.tool_responses)
    live.push(audio_msg(b"\x00\x00"))  # the model answers the tool result
    live.push(turn_complete_msg())
    await asyncio.sleep(0.5)
    assert len(connector.sessions) == 1
    assert not [e for e in session.metrics.events() if e["kind"] == "stall"]
    await session.shutdown()


async def test_stall_without_handle_replays_context_in_fresh_session(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path, stall_timeout_s=0.3)
    await start(session, transport)
    first = connector.sessions[0]
    first.push(turn_complete_msg())  # greeting answered
    await wait_until(lambda: transport.events("turn_complete"))
    session._last_form = {"entity": "Contact", "fields": {"Last Name": "Okafor"}}
    await session.send_typed_text("her email is maya@example.com")
    await wait_until(lambda: len(connector.sessions) == 2, timeout=3)
    assert connector.configs[1].session_resumption.handle is None
    second = connector.sessions[1]
    await wait_until(lambda: second.texts)
    note = second.texts[0]
    assert "maya@example.com" in note and "Okafor" in note and "get_screen" in note
    await session.shutdown()


async def test_stall_gives_up_after_two_recoveries_without_ending(tmp_path):
    session, transport, connector, _, _ = make_session(tmp_path, stall_timeout_s=0.2)
    await start(session, transport)
    connector.sessions[0].push(resumption_msg("h1"))
    await wait_until(lambda: transport.events("error"), timeout=4)
    assert "say that again" in transport.events("error")[0]["message"]
    assert len(connector.sessions) == 3  # two recoveries, then no more
    await asyncio.sleep(0.5)
    assert len(connector.sessions) == 3 and not session.closed
    await session.shutdown()
