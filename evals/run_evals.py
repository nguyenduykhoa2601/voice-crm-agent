"""Scripted multi-turn evals against the real server (typed-text mode).

What it does
- Starts the real FastAPI server as a subprocess on a free port (or connects to --url).
- Runs each scenario from evals/scenarios.json in its own WebSocket session, sequentially,
  with the mic off: user lines are sent as typed text, so runs are reproducible.
- Seeds / verifies / cleans up CRM records through a separate logged-in CrmSession that
  calls the EspoCRM REST API (never through the agent).
- Checks per turn (tools_any soft, tools_none hard, must_not_claim_success hard), the end
  state (success / expect_count / forbid / no unexpected records), and records latency.
- Writes evals/results/<timestamp>.json and regenerates evals/REPORT.md.

Usage
    uv run python -m evals.run_evals                         # full catalogue, 1 run
    uv run python -m evals.run_evals --scenarios S01,S03 --runs 5
    uv run python -m evals.run_evals --tags happy_path,correction --runs 3
    uv run python -m evals.run_evals --url ws://127.0.0.1:7860/ws   # use a running server
    uv run python -m evals.run_evals --report-from evals/results/a.json,evals/results/b.json

Special user lines
- "<silence Ns>": send nothing for N seconds (the server's idle nudge should fire once).
- "<barge_in> text": the text is sent while the agent is still answering the previous turn
  (right after its first audio chunk once its tools have settled); the turn must show an
  `interrupted` event or the agent must change course (call one of the turn's tools_any).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import random
import re
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.metrics import percentile  # noqa: E402

EVALS_DIR = ROOT / "evals"
SCENARIOS_PATH = EVALS_DIR / "scenarios.json"
RESULTS_DIR = EVALS_DIR / "results"
REPORT_PATH = EVALS_DIR / "REPORT.md"
NOTES_PATH = EVALS_DIR / "report_notes.md"

log = logging.getLogger("evals")

QUIET_S = 2.5  # agent considered done after this long without activity (and turn_complete)
TURN_TIMEOUT_S = 120.0
NO_RESPONSE_S = 30.0
READY_TIMEOUT_S = 90.0

SILENCE_RE = re.compile(r"^<silence\s+(\d+(?:\.\d+)?)s>$", re.I)
BARGE_RE = re.compile(r"^<barge_in>\s*", re.I)

# Claims of completion. "updated" only counts when it refers to a record, not a form field.
CLAIM_RE = re.compile(
    r"\b(saved|created|moved|all set|done|updated (?:the |your |her |his |its )?"
    r"(?:record|contact|opportunity|stage|deal))\b",
    re.I,
)
# Words that, shortly before a claim word, make it a non-claim ("not saved", "once it's saved").
NEGATORS = {
    "not", "never", "nothing", "no", "n't", "yet", "will", "'ll", "once", "before", "until",
    "when", "after", "if", "be", "been", "get", "gets", "is", "it's", "isn't", "wasn't", "haven't",
    "hasn't", "without", "to", "unsaved", "can", "could", "should", "shall", "would", "want",
}
SUCCESS_TOOLS = {"save", "set_stage"}

# First-person past-tense claims of a UI action ("I've opened the form", "I entered the email").
# Each verb must be backed by an ok tool call of a matching kind in the same turn.
ACTION_CLAIM_RE = re.compile(
    r"\b(?:i've|i have|i)\s+(?:now\s+|just\s+|also\s+|already\s+)?"
    r"(opened|entered|filled(?: in)?|added|typed|changed|updated|corrected|set|put|cancelled|canceled)\b",
    re.I,
)
ACTION_TOOLS = {
    "opened": {"open_create_form", "open_record", "edit_record", "find_records"},
    "cancelled": {"cancel"}, "canceled": {"cancel"},
}
FILL_TOOLS = {"fill_fields", "set_stage", "save", "open_create_form", "edit_record"}


# ----------------------------------------------------------------------------- helpers

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def make_rid() -> str:
    return "e" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def subst(obj: Any, rid: str) -> Any:
    if isinstance(obj, str):
        return obj.replace("{rid}", rid)
    if isinstance(obj, list):
        return [subst(x, rid) for x in obj]
    if isinstance(obj, dict):
        return {k: subst(v, rid) for k, v in obj.items()}
    return obj


def digits(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


def same_value(field_name: str, actual: Any, expected: Any) -> bool:
    if "phone" in field_name.lower():
        a, e = digits(actual), digits(expected)
        return bool(e) and (a == e or a.endswith(e))
    return str(actual or "").strip().lower() == str(expected or "").strip().lower()


def find_claims(text: str) -> list[str]:
    """Return completion claims in `text`, ignoring negated / conditional uses."""
    out = []
    for m in CLAIM_RE.finditer(text):
        before = re.findall(r"[a-z']+", text[max(0, m.start() - 40): m.start()].lower())[-3:]
        if any(w in NEGATORS or w.endswith("n't") for w in before):
            continue
        # Questions ("Shall I save? Is it done?") are not claims.
        tail = text[m.end(): m.end() + 60]
        sentence_end = re.search(r"[.!?]", tail)
        if sentence_end and sentence_end.group(0) == "?":
            continue
        out.append(text[max(0, m.start() - 30): m.end() + 20].strip())
    return out


def unbacked_action_claims(text: str, ok_tools: set[str]) -> list[str]:
    """Action claims in `text` with no ok tool call of a matching kind in the turn."""
    out = []
    for m in ACTION_CLAIM_RE.finditer(text):
        verb = m.group(1).lower().split()[0]
        needed = ACTION_TOOLS.get(verb, FILL_TOOLS)
        if not ok_tools & needed:
            out.append(text[max(0, m.start() - 10): m.end() + 40].strip())
    return out


def performed_tools(actions: list[dict]) -> set[str]:
    """Tools that did something on screen: ok, or a fill that partly succeeded (read-back shown)."""
    return {a["name"] for a in actions
            if a.get("status") == "ok" or "Form now shows:" in (a.get("summary") or "")}


def rescore(r: dict) -> dict:
    """Re-apply the unbacked-action rule to a stored result (keeps reports consistent with the rules)."""
    hard = [h for h in r["hard_failures"] if "claimed an action no tool performed" not in h]
    claims = [c for c in r.get("claims", []) if c.get("kind") != "unbacked_action"]
    for t in r["transcript"]:
        text = " ".join(t["agent"])
        found = unbacked_action_claims(text, performed_tools(t["tools"]))
        if found:
            claims.append({"turn": t["index"] + 1, "claims": found, "hard": True, "kind": "unbacked_action"})
            hard.append(f"T{t['index'] + 1}: claimed an action no tool performed: {found[0]!r}")
    r = {**r, "hard_failures": hard, "claims": claims,
         "unbacked_action_claims": sum(1 for c in claims if c.get("kind") == "unbacked_action")}
    r["passed"] = not hard and not r["infra_failures"]
    r["category"] = None if r["passed"] else classify(r)
    return r


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def words(text: str) -> int:
    return len(re.findall(r"\b[\w'@.+-]+\b", text))


# ----------------------------------------------------------------------------- server

class ServerProc:
    """The real server as a subprocess; always stopped in `stop()`."""

    def __init__(self, workdir: Path, port: int | None = None) -> None:
        self.port = port or free_port()
        self.workdir = workdir
        self.proc: subprocess.Popen | None = None
        self.log_path = workdir / f"server-{self.port}.log"
        self._log_fh = None

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def start(self, timeout_s: float = 120.0) -> None:
        env = dict(os.environ)
        # Keep eval traffic out of the server's real metrics file.
        env["METRICS_PATH"] = str(self.workdir / "server-metrics.jsonl")
        env.setdefault("HEADLESS", "1")
        env["MAX_SESSIONS"] = "1"
        self._log_fh = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.server:app", "--host", "127.0.0.1",
             "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=self._log_fh, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early (code {self.proc.returncode}); see server log")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2) as r:
                    health = json.loads(r.read())
                if health.get("browser"):
                    return
                if health.get("browser_error"):
                    raise RuntimeError(f"server browser failed: {health['browser_error']}")
            except (OSError, ValueError):
                pass
            time.sleep(1.0)
        raise RuntimeError("server did not become healthy in time")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None


# ----------------------------------------------------------------------------- CRM verifier

class CrmVerifier:
    """Separate logged-in CRM session for seeding, verification and cleanup (REST API)."""

    SELECT = {
        "Contact": "firstName,lastName,name,emailAddress,phoneNumber,title,createdAt",
        "Opportunity": "name,stage,createdAt",
    }

    def __init__(self) -> None:
        self.browser = None
        self.session = None

    async def start(self) -> None:
        from app.crm.browser import CrmBrowser

        self.browser = CrmBrowser()
        self.session = await self.browser.new_session(on_frame=None, login=True)

    async def close(self) -> None:
        if self.browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.browser.close(), 20)
        self.browser = self.session = None

    async def api(self, method: str, path: str, body: Any = None) -> Any:
        from app.crm.browser import CrmError

        for attempt in range(3):
            try:
                return await self.session.api(method, path, body)
            except CrmError as exc:
                if attempt == 2 or "HTTP 4" in str(exc) and "401" not in str(exc) and "unauthorized" not in str(exc):
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))

    async def query(self, entity: str, lookup: dict[str, str]) -> list[dict]:
        parts = [f"maxSize=50&select={self.SELECT.get(entity, 'name,createdAt')}"]
        for i, (attr, value) in enumerate(lookup.items()):
            parts.append(
                f"where[{i}][type]=equals&where[{i}][attribute]={quote(attr)}&where[{i}][value]={quote(str(value))}"
            )
        data = await self.api("GET", f"{entity}?" + "&".join(parts))
        return list((data or {}).get("list", []))

    async def recent(self, entity: str, since: dt.datetime) -> list[dict]:
        data = await self.api(
            "GET", f"{entity}?maxSize=60&orderBy=createdAt&order=desc&select={self.SELECT.get(entity, 'name,createdAt')}"
        )
        cutoff = since.strftime("%Y-%m-%d %H:%M:%S")
        return [r for r in (data or {}).get("list", []) if str(r.get("createdAt") or "") >= cutoff]

    async def seed_opportunity(self, rid: str) -> dict:
        from app.crm.tools import create_seed_opportunity

        return await create_seed_opportunity(self.session, rid)

    async def delete(self, entity: str, record_id: str) -> bool:
        from app.crm.tools import delete_record

        return await delete_record(self.session, entity, record_id)


# ----------------------------------------------------------------------------- run records

@dataclass
class Turn:
    index: int
    user: str
    kind: str = "text"  # text | silence | barge_in
    expect: dict = field(default_factory=dict)
    t_send: float = 0.0
    t_end: float = 0.0
    first_audio_client_ms: float | None = None
    server_metrics: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)  # completed action events
    running: dict[str, float] = field(default_factory=dict)
    agent_chunks: list[tuple[float, str]] = field(default_factory=list)
    utterances: list[str] = field(default_factory=list)
    interrupted: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn_completes: int = 0
    last_activity: float = 0.0
    last_turn_complete: float = 0.0
    last_action_event: float = 0.0
    audio_chunks: int = 0
    timed_out: bool = False
    barge_sent_mid_speech: bool | None = None

    @property
    def agent_text(self) -> str:
        return "".join(t for _, t in self.agent_chunks).strip()

    def tools(self) -> list[str]:
        return [a["name"] for a in self.actions]

    def to_json(self, t0: float) -> dict:
        sm = self.server_metrics[0] if self.server_metrics else {}
        return {
            "index": self.index,
            "user": self.user,
            "kind": self.kind,
            "agent": self.utterances or ([self.agent_text] if self.agent_text else []),
            "tools": [
                {"name": a["name"], "args": a.get("args"), "status": a.get("status"),
                 "ms": a.get("ms"), "summary": (a.get("summary") or "")[:220]}
                for a in self.actions
            ],
            "first_audio_ms_client": None if self.first_audio_client_ms is None else round(self.first_audio_client_ms),
            "first_audio_ms_server": sm.get("first_audio_ms"),
            "turn_ms_server": sm.get("turn_ms"),
            "tool_ms_server": sm.get("tool_ms"),
            "interrupted": bool(self.interrupted),
            "barge_in_stop_ms": (round((self.interrupted[0] - self.t_send) * 1000)
                                 if self.interrupted and self.kind == "barge_in" else None),
            "barge_sent_mid_speech": self.barge_sent_mid_speech,
            "errors": self.errors,
            "duration_ms": round((self.t_end - self.t_send) * 1000) if self.t_end else None,
            "timed_out": self.timed_out,
            "start_s": round(self.t_send - t0, 1),
        }


class WsRun:
    """One scenario over one WebSocket session."""

    def __init__(self, url: str, quiet_s: float = QUIET_S) -> None:
        self.url = url
        self.quiet_s = quiet_s
        self.ws = None
        self.t0 = time.monotonic()
        self.turn: Turn = Turn(index=-1, user="<greeting>", kind="greeting")
        self.turns: list[Turn] = []
        self.status: list[dict] = []
        self.model: str | None = None
        self.ready = asyncio.Event()
        self.ended = asyncio.Event()
        self.frames = 0
        self.audio_event = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self._partial = ""
        self.next_barge_mid: bool | None = None
        self.stopping = False

    async def __aenter__(self) -> "WsRun":
        from websockets.asyncio.client import connect

        self.ws = await connect(self.url, max_size=None, open_timeout=20, ping_interval=20)
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        self.stopping = True
        with contextlib.suppress(Exception):
            await self.ws.send(json.dumps({"type": "stop"}))
            await asyncio.sleep(0.5)
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
        with contextlib.suppress(Exception):
            await self.ws.close()

    def _flush_partial(self) -> None:
        if self._partial.strip():
            self.turn.utterances.append(self._partial.strip())
        self._partial = ""

    async def _read(self) -> None:
        try:
            async for msg in self.ws:
                now = time.monotonic()
                t = self.turn
                if isinstance(msg, (bytes, bytearray)):
                    if msg[:1] == b"\x01":
                        t.audio_chunks += 1
                        t.last_activity = now
                        if t.first_audio_client_ms is None and t.t_send:
                            t.first_audio_client_ms = (now - t.t_send) * 1000
                        self.audio_event.set()
                    else:
                        self.frames += 1
                    continue
                ev = json.loads(msg)
                ty = ev.get("type")
                if self.stopping:
                    continue
                if ty == "status":
                    self.status.append({k: v for k, v in ev.items() if k != "type"})
                    st = ev.get("state")
                    if st == "ready":
                        m = re.search(r"\(([^)]+)\)", ev.get("message") or "")
                        self.model = m.group(1) if m else None
                        self.ready.set()
                    elif st in ("ended", "busy"):
                        t.errors.append(f"status {st}: {ev.get('message')}")
                        self.ended.set()
                        self.ready.set()
                    continue
                t.last_activity = now
                if ty == "transcript" and ev.get("role") == "agent":
                    text = ev.get("text") or ""
                    t.agent_chunks.append((now, text))
                    self._partial += text
                    if ev.get("final"):
                        self._flush_partial()
                elif ty == "action":
                    t.last_action_event = now
                    if ev.get("status") == "running":
                        t.running[ev.get("id")] = now
                    else:
                        t.running.pop(ev.get("id"), None)
                        t.actions.append({**ev, "t": now})
                elif ty == "turn_complete":
                    self._flush_partial()
                    t.turn_completes += 1
                    t.last_turn_complete = now
                elif ty == "interrupted":
                    self._flush_partial()
                    t.interrupted.append(now)
                elif ty == "metrics":
                    t.server_metrics.append(ev)
                elif ty == "error":
                    t.errors.append(str(ev.get("message")))
        except Exception as exc:  # connection closed
            self.turn.errors.append(f"ws closed: {exc.__class__.__name__}")
            self.ended.set()
            self.ready.set()

    def settled(self, now: float) -> bool:
        t = self.turn
        if t.running:
            return False
        if t.turn_completes == 0:
            return False
        if t.last_turn_complete < t.last_action_event:
            return False
        return now - max(t.last_activity, t.last_turn_complete) >= self.quiet_s

    async def wait_settled(self, timeout_s: float = TURN_TIMEOUT_S) -> None:
        t = self.turn
        start = time.monotonic()
        while not self.ended.is_set():
            now = time.monotonic()
            if self.settled(now):
                break
            if now - start > timeout_s:
                t.timed_out = True
                break
            # Nothing at all came back: give up on this turn.
            if t.last_activity == 0 and t.turn_completes == 0 and now - start > NO_RESPONSE_S:
                t.timed_out = True
                t.errors.append("no agent response")
                break
            await asyncio.sleep(0.1)
        t.t_end = time.monotonic()

    async def wait_for_barge_point(self, timeout_s: float = 45.0) -> bool:
        """Wait until the agent is speaking with its tools settled. True if mid-speech."""
        t = self.turn
        expects_tools = bool(t.expect.get("tools_any"))
        start = time.monotonic()
        while time.monotonic() - start < timeout_s and not self.ended.is_set():
            self.audio_event.clear()
            try:
                await asyncio.wait_for(self.audio_event.wait(), 1.0)
            except asyncio.TimeoutError:
                continue
            if t.running:
                continue
            if expects_tools and not t.actions:
                continue
            return True
        return False

    def begin_turn(self, turn: Turn) -> None:
        self._flush_partial()
        self.turn.t_end = self.turn.t_end or time.monotonic()
        self.turn = turn
        turn.t_send = time.monotonic()
        turn.last_activity = 0.0
        self.turns.append(turn)

    async def send_text(self, text: str) -> None:
        await self.ws.send(json.dumps({"type": "text", "text": text}))


# ----------------------------------------------------------------------------- scenario run

def track_corrections(turns: list[Turn]) -> dict[str, int]:
    """Same rules as the server's Outcome tracker, from action events."""
    current: dict[str, str] = {}
    corrections = cancels = saves = 0
    for t in turns:
        for a in t.actions:
            name, args, ok = a["name"], a.get("args") or {}, a.get("status") == "ok"
            if name in ("open_create_form", "open_record") and ok:
                current = {}
            if name == "fill_fields" or (name == "open_create_form" and args.get("fields")):
                fields = args.get("fields", args)
                pairs = ([(f.get("field"), f.get("value")) for f in fields if isinstance(f, dict)]
                         if isinstance(fields, list) else list(fields.items()) if isinstance(fields, dict) else [])
                for k, v in pairs:
                    new = str(v).strip().lower()
                    if k in current and current[k] != new:
                        corrections += 1
                    current[k] = new
            elif name == "set_stage":
                stage = str(args.get("stage", "")).strip().lower()
                if "stage" in current and current["stage"] != stage:
                    corrections += 1
                current["stage"] = stage
                saves += ok
            elif name == "save" and ok:
                saves += 1
            elif name == "cancel" and ok:
                cancels += 1
                current = {}
    return {"corrections": corrections, "cancels": cancels, "interventions": corrections + cancels,
            "saves": saves}


def scenario_names(sc: dict) -> set[str]:
    """Capitalised name-like words from the user lines (to spot unexpected Contacts)."""
    names: set[str] = set()
    for t in sc["turns"]:
        for w in re.findall(r"\b[A-Z][a-z]{2,}\b", t.get("user", "")):
            names.add(w.lower())
    return names


async def check_end_state(sc: dict, verifier: CrmVerifier, since: dt.datetime,
                          seeded: list[tuple[str, str]]) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Returns (hard_failures, notes, records_to_delete)."""
    fails: list[str] = []
    notes: list[str] = []
    delete: list[tuple[str, str]] = list(seeded)
    known_ids: set[str] = {i for _, i in seeded}

    succ = sc.get("success")
    if succ:
        recs = await verifier.query(succ["entity"], succ["lookup"])
        for r in recs:
            if r["id"] not in known_ids:
                delete.append((succ["entity"], r["id"]))
                known_ids.add(r["id"])
        if not recs:
            fails.append(f"end-state: no {succ['entity']} matching {succ['lookup']}")
        else:
            if "expect_count" in succ and len(recs) != succ["expect_count"]:
                fails.append(f"end-state: expected {succ['expect_count']} {succ['entity']} record(s), found {len(recs)}")
            best_diff = None
            for r in recs:
                diff = [f"{k}={r.get(k)!r} (want {v!r})" for k, v in succ.get("fields", {}).items()
                        if not same_value(k, r.get(k), v)]
                if best_diff is None or len(diff) < len(best_diff):
                    best_diff = diff
            if best_diff:
                fails.append("end-state field mismatch: " + "; ".join(best_diff))

    forbid = sc.get("forbid")
    if forbid:
        recs = await verifier.query(forbid["entity"], forbid["lookup"])
        for r in recs:
            if r["id"] not in known_ids:
                delete.append((forbid["entity"], r["id"]))
                known_ids.add(r["id"])
        if recs:
            fails.append(f"forbidden {forbid['entity']} exists: {forbid['lookup']}")

    # Records the agent created that the scenario did not ask for (e.g. a dropped contact saved).
    names = scenario_names(sc)
    rid = sc["_rid"]
    for entity in ("Contact", "Opportunity"):
        try:
            recent = await verifier.recent(entity, since)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"could not list recent {entity}: {exc}")
            continue
        for r in recent:
            if r["id"] in known_ids:
                continue
            blob = " ".join(str(r.get(k) or "") for k in ("name", "firstName", "lastName", "emailAddress"))
            low = blob.lower()
            mine = rid in low or (entity == "Contact" and str(r.get("lastName") or "").lower() in names
                                  and str(r.get("lastName") or ""))
            if mine:
                delete.append((entity, r["id"]))
                known_ids.add(r["id"])
                fails.append(f"unexpected {entity} created: {blob.strip()!r}")
    return fails, notes, delete


async def run_scenario(sc_raw: dict, url: str, verifier: CrmVerifier, run_no: int,
                       quiet_s: float = QUIET_S) -> dict:
    rid = make_rid()
    sc = subst(sc_raw, rid)
    sc["_rid"] = rid
    started_wall = now_utc() - dt.timedelta(seconds=90)  # CRM clock skew margin
    t_start = time.monotonic()
    hard: list[str] = []
    soft: list[str] = []
    infra: list[str] = []
    seeded: list[tuple[str, str]] = []
    claims_log: list[dict] = []
    run_turns: list[Turn] = []
    model = None
    t0 = time.monotonic()

    # Seed
    if "Seed Opportunity" in str(sc.get("setup", "")):
        try:
            rec = await verifier.seed_opportunity(rid)
            seeded.append(("Opportunity", rec["id"]))
        except Exception as exc:  # noqa: BLE001
            infra.append(f"seed failed: {exc}")

    if not infra:
        try:
            async with WsRun(url, quiet_s=quiet_s) as run:
                t0 = run.t0
                await run.ws.send(json.dumps({"type": "start"}))
                await run.ws.send(json.dumps({"type": "mic", "on": False}))
                try:
                    await asyncio.wait_for(run.ready.wait(), READY_TIMEOUT_S)
                except asyncio.TimeoutError:
                    infra.append("session did not become ready")
                model = run.model
                if run.ended.is_set() or not run.model:
                    infra.append("session start failed: " + "; ".join(run.turn.errors or ["unknown"]))
                else:
                    # Greeting
                    run.turn.t_send = time.monotonic()
                    await run.wait_settled(60)
                    turns = sc["turns"]
                    for i, spec in enumerate(turns):
                        line = spec.get("user", "")
                        m_sil = SILENCE_RE.match(line.strip())
                        is_barge = bool(BARGE_RE.match(line))
                        text = BARGE_RE.sub("", line).strip()
                        turn = Turn(index=i, user=line, expect=spec.get("expect", {}),
                                    kind="silence" if m_sil else "barge_in" if is_barge else "text")
                        run.begin_turn(turn)
                        if m_sil:
                            await asyncio.sleep(float(m_sil.group(1)))
                            # The nudge may still be in flight: let it finish.
                            if turn.last_activity:
                                await run.wait_settled(40)
                            turn.t_end = time.monotonic()
                        else:
                            await run.send_text(text)
                            nxt = turns[i + 1]["user"] if i + 1 < len(turns) else ""
                            if BARGE_RE.match(nxt):
                                run.next_barge_mid = await run.wait_for_barge_point()
                                turn.t_end = time.monotonic()
                            else:
                                await run.wait_settled()
                        if turn.kind == "barge_in":
                            turn.barge_sent_mid_speech = run.next_barge_mid
                        if run.ended.is_set():
                            infra.append("session ended mid-scenario: " + "; ".join(turn.errors))
                            break
                    run_turns = run.turns
                    # Let late tool results land, then end.
                    await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            infra.append(f"websocket error: {exc.__class__.__name__}: {exc}")

    # ---- per-turn checks
    for t in run_turns:
        e = t.expect
        tools = t.tools()
        if t.timed_out:
            soft.append(f"T{t.index + 1}: turn timed out waiting for the agent")
        if e.get("tools_any") and not set(tools) & set(e["tools_any"]):
            soft.append(f"T{t.index + 1}: none of {e['tools_any']} called (got {tools or 'no tools'})")
        bad = sorted(set(tools) & set(e.get("tools_none", [])))
        if bad:
            hard.append(f"T{t.index + 1}: forbidden tool(s) called: {bad}")
        ok_success = [a["t"] for a in t.actions if a["name"] in SUCCESS_TOOLS and a.get("status") == "ok"]
        cutoff = ok_success[0] if ok_success else float("inf")
        pre_text = "".join(txt for ts, txt in t.agent_chunks if ts < cutoff)
        claims = find_claims(pre_text)
        if claims:
            claims_log.append({"turn": t.index + 1, "claims": claims, "hard": bool(e.get("must_not_claim_success"))})
            if e.get("must_not_claim_success"):
                hard.append(f"T{t.index + 1}: claimed success before a verified save: {claims[0]!r}")
        unbacked = unbacked_action_claims(t.agent_text, performed_tools(t.actions))
        if unbacked:
            claims_log.append({"turn": t.index + 1, "claims": unbacked, "hard": True, "kind": "unbacked_action"})
            hard.append(f"T{t.index + 1}: claimed an action no tool performed: {unbacked[0]!r}")
        for err in t.errors:
            if any(k in err.lower() for k in ("free-tier", "quota", "429", "unavailable", "rejected")):
                infra.append(f"T{t.index + 1}: {err}")
            else:
                soft.append(f"T{t.index + 1}: error event: {err}")
        if t.kind == "barge_in":
            changed = bool(set(tools) & set(e.get("tools_any", [])))
            if not t.interrupted and not changed:
                hard.append(f"T{t.index + 1}: barge-in neither interrupted the agent nor changed its course")
            elif not t.interrupted:
                soft.append(f"T{t.index + 1}: no `interrupted` event on barge-in (agent did change course)")
        if t.kind == "silence":
            nudges = len([u for u in t.utterances if u])
            if nudges == 0:
                soft.append(f"T{t.index + 1}: no nudge during silence")
            elif nudges > 1:
                hard.append(f"T{t.index + 1}: {nudges} nudges during silence (expected one)")
        for a in t.actions:
            if a.get("status") == "error" and re.search(r"timed out|HTTP 5\d\d|failed unexpectedly|"
                                                        r"Target closed|net::", a.get("summary") or "", re.I):
                infra.append(f"T{t.index + 1}: tool {a['name']} infra error: {(a.get('summary') or '')[:120]}")

    # ---- end state + cleanup
    delete: list[tuple[str, str]] = list(seeded)
    end_fails: list[str] = []
    if verifier.session is not None:
        try:
            await asyncio.sleep(1.0)
            end_fails, notes, delete = await check_end_state(sc, verifier, started_wall, seeded)
            soft.extend(notes)
        except Exception as exc:  # noqa: BLE001
            infra.append(f"end-state check failed: {exc}")
    hard.extend(end_fails)

    # False success: a hard-turn claim, or a final success claim while the end state is wrong.
    false_success = any(c["hard"] and c.get("kind") != "unbacked_action" for c in claims_log)
    unbacked_claims = sum(1 for c in claims_log if c.get("kind") == "unbacked_action")
    success_claims = [c for c in claims_log if c.get("kind") != "unbacked_action"]
    last_turn_claim = bool(success_claims and run_turns and success_claims[-1]["turn"] == run_turns[-1].index + 1)
    if end_fails and last_turn_claim and sc.get("success"):
        false_success = True
        hard.append("agent claimed success but the CRM end state does not match")

    cleaned = 0
    for entity, rec_id in delete:
        with contextlib.suppress(Exception):
            cleaned += await verifier.delete(entity, rec_id)

    counts = track_corrections(run_turns)
    tool_events = [a for t in run_turns for a in t.actions]
    passed = not hard and not infra
    utter = [u for t in run_turns for u in t.utterances]
    result = {
        "scenario": sc["id"],
        "title": sc["title"],
        "tags": sc.get("tags", []),
        "workflow": sc.get("workflow"),
        "run": run_no,
        "rid": rid,
        "model": model,
        "passed": passed,
        "infra": bool(infra),
        "hard_failures": hard,
        "infra_failures": infra,
        "soft_warnings": soft,
        "false_success": false_success,
        "unbacked_action_claims": unbacked_claims,
        "claims": claims_log,
        "turns": len(run_turns),
        "tool_calls": len(tool_events),
        "tool_errors": sum(1 for a in tool_events if a.get("status") != "ok"),
        "tools": [{"name": a["name"], "ms": a.get("ms"), "ok": a.get("status") == "ok"} for a in tool_events],
        **counts,
        "utterances": len(utter),
        "utterances_over_25_words": sum(1 for u in utter if words(u) > 25),
        "duration_s": round(time.monotonic() - t_start, 1),
        "records_cleaned": cleaned,
        "transcript": [t.to_json(t0) for t in run_turns],
    }
    result["category"] = classify(result) if not passed else None
    return result


def classify(r: dict) -> str:
    """Root-cause bucket for a failed run (heuristic; manual notes can override in the report)."""
    if r["infra"]:
        return "infra"
    hard = " | ".join(r["hard_failures"])
    if r["false_success"] or "claimed success" in hard or "claimed an action" in hard:
        return "misread screen"
    if "forbidden tool" in hard or "unexpected" in hard or "forbidden " in hard or "barge-in" in hard:
        return "wrong action"
    if "field mismatch" in hard:
        # Did the agent type the wrong value (misheard / misparsed the user) or never act on it?
        filled = json.dumps([tool.get("args") for t in r["transcript"] for tool in t["tools"]
                             if tool["name"] == "fill_fields"]).lower()
        m = re.findall(r"=('[^']*'|None) \(want", hard)
        if m and any(v.strip("'").lower() and v.strip("'").lower() in filled for v in m):
            return "misunderstood user"
        return "wrong action"
    if "no " in hard and "matching" in hard:
        return "wrong action"
    return "wrong action"


# ----------------------------------------------------------------------------- report

def _p(values: list[float], pct: float) -> str:
    v = percentile(values, pct)
    return "–" if v is None else f"{v:,.0f}"


def aggregate(results: list[dict]) -> dict:
    by_sc: dict[str, list[dict]] = {}
    for r in results:
        by_sc.setdefault(r["scenario"], []).append(r)
    fa_server = [t["first_audio_ms_server"] for r in results for t in r["transcript"]
                 if t.get("first_audio_ms_server") is not None and t["kind"] != "silence"]
    fa_client = [t["first_audio_ms_client"] for r in results for t in r["transcript"]
                 if t.get("first_audio_ms_client") is not None and t["kind"] == "text"]
    fa_tool = [t["first_audio_ms_server"] for r in results for t in r["transcript"]
               if t.get("first_audio_ms_server") is not None and t["kind"] == "text" and t["tools"]]
    fa_talk = [t["first_audio_ms_server"] for r in results for t in r["transcript"]
               if t.get("first_audio_ms_server") is not None and t["kind"] == "text" and not t["tools"]]
    tools: dict[str, list[dict]] = {}
    for r in results:
        for tl in r["tools"]:
            tools.setdefault(tl["name"], []).append(tl)
    scored = [r for r in results if not r["infra"]]
    happy = [r for r in scored if r["scenario"] in ("S01", "S02", "S16", "S20")]
    return {
        "runs": len(results),
        "scored_runs": len(scored),
        "infra_runs": len(results) - len(scored),
        "passed": sum(r["passed"] for r in results),
        "pass_rate": round(100 * sum(r["passed"] for r in scored) / len(scored), 1) if scored else None,
        "happy_path_rate": round(100 * sum(r["passed"] for r in happy) / len(happy), 1) if happy else None,
        "false_success": sum(r["false_success"] for r in results),
        "unbacked_action_claims": sum(r.get("unbacked_action_claims", 0) for r in results),
        "first_audio_server_ms": {"p50": percentile(fa_server, 50), "p90": percentile(fa_server, 90),
                                  "p95": percentile(fa_server, 95), "n": len(fa_server)},
        "first_audio_client_ms": {"p50": percentile(fa_client, 50), "p90": percentile(fa_client, 90),
                                  "p95": percentile(fa_client, 95), "n": len(fa_client)},
        "first_audio_tool_turns_ms": {"p50": percentile(fa_tool, 50), "p90": percentile(fa_tool, 90), "n": len(fa_tool)},
        "first_audio_talk_turns_ms": {"p50": percentile(fa_talk, 50), "p90": percentile(fa_talk, 90), "n": len(fa_talk)},
        "interventions_per_run": round(sum(r["interventions"] for r in scored) / len(scored), 2) if scored else None,
        "tools": {
            name: {"calls": len(rows), "ok_rate": round(100 * sum(x["ok"] for x in rows) / len(rows), 1),
                   "p50_ms": percentile([x["ms"] for x in rows if x.get("ms") is not None], 50),
                   "p90_ms": percentile([x["ms"] for x in rows if x.get("ms") is not None], 90)}
            for name, rows in sorted(tools.items())
        },
        "scenarios": {
            sid: {
                "title": rows[0]["title"], "runs": len(rows), "passed": sum(r["passed"] for r in rows),
                "infra": sum(r["infra"] for r in rows),
            }
            for sid, rows in sorted(by_sc.items())
        },
    }


def excerpt(r: dict, max_turns: int = 3) -> str:
    """Transcript lines around the first failing turn."""
    m = re.search(r"\bT(\d+)\b", " ".join(r["hard_failures"] + r["infra_failures"]))
    idx = int(m.group(1)) - 1 if m else len(r["transcript"]) - 1
    lo = max(0, idx - max_turns + 1)
    lines = []
    for t in r["transcript"][lo: idx + 1]:
        lines.append(f"USER: {t['user']}")
        for tl in t["tools"]:
            args = json.dumps(tl["args"], ensure_ascii=False)
            lines.append(f"  [{tl['name']} {tl['status']} {tl['ms']}ms] {args[:140]} -> {tl['summary'][:140]}")
        for u in t["agent"]:
            lines.append(f"AGENT: {u}")
    return "\n".join(lines)


def baseline_section(results: list[dict], baseline: list[dict], label: str) -> list[str]:
    """Before/after table: baseline runs (before agent fixes) vs the main runs."""
    a, b = aggregate(baseline), aggregate(results)
    out = ["## Before vs after the agent fixes", "",
           f"Baseline: {label} ({a['runs']} runs, prompt/tools before the fixes listed below). "
           "After: the runs this report is built from.", "",
           "| Metric | Before | After |", "|---|---|---|",
           f"| Pass rate | {a['pass_rate']}% ({a['passed']}/{a['scored_runs']}) | {b['pass_rate']}% ({b['passed']}/{b['scored_runs']}) |",
           f"| False-success claims | {a['false_success']} | {b['false_success']} |",
           f"| Unbacked action claims | {a['unbacked_action_claims']} | {b['unbacked_action_claims']} |",
           f"| First audio p50 / p90 (ms) | {_fmt(a['first_audio_server_ms']['p50'])} / {_fmt(a['first_audio_server_ms']['p90'])} | "
           f"{_fmt(b['first_audio_server_ms']['p50'])} / {_fmt(b['first_audio_server_ms']['p90'])} |"]
    sids = sorted(set(a["scenarios"]) | set(b["scenarios"]))
    out += ["", "| Scenario | Before (pass/runs) | After (pass/runs) |", "|---|---|---|"]
    for sid in sids:
        x, y = a["scenarios"].get(sid), b["scenarios"].get(sid)
        out.append(f"| {sid} | {f'{x['passed']}/{x['runs']}' if x else '–'} | {f'{y['passed']}/{y['runs']}' if y else '–'} |")
    out.append("")
    return out


def write_report(results: list[dict], sources: list[str], baseline: list[dict] | None = None,
                 baseline_label: str = "") -> str:
    agg = aggregate(results)
    models = sorted({r["model"] for r in results if r.get("model")})
    fs, fc = agg["first_audio_server_ms"], agg["first_audio_client_ms"]
    by_sc: dict[str, list[dict]] = {}
    for r in results:
        by_sc.setdefault(r["scenario"], []).append(r)

    out: list[str] = []
    out.append("# Eval report")
    out.append("")
    out.append(f"Generated {now_utc().strftime('%Y-%m-%d %H:%M UTC')} by `evals/run_evals.py` from "
               f"{len(sources)} result file(s): {', '.join(sources)}. Live model: {', '.join(models) or 'n/a'}.")
    out.append("")
    out.append("Method: the real server is started as a subprocess; each scenario runs in its own WebSocket "
               "session with the mic off (user lines are typed text, so runs are reproducible), sequentially, "
               "against the public EspoCRM demo. End state is verified through the CRM REST API from a separate "
               "logged-in session, then every record the run created is deleted.")
    out.append("")
    out.append("## Headline")
    out.append("")
    out.append("| Metric | Value | Target |")
    out.append("|---|---|---|")
    out.append(f"| Scenario runs (scored / infra-excluded) | {agg['runs']} ({agg['scored_runs']} / {agg['infra_runs']}) | |")
    out.append(f"| Pass rate, all scored runs | {agg['pass_rate']}% | ≥ 80% (NF-08) |")
    out.append(f"| Happy-path completion (S01, S02, S16, S20) | {agg['happy_path_rate']}% | ≥ 90% (NF-07) |")
    out.append(f"| False-success claims (said saved/moved/done before a verified save) | {agg['false_success']} | 0 (hard gate) |")
    out.append(f"| Unbacked action claims (\"I've opened / entered ...\" with no ok tool call that turn) | "
               f"{agg['unbacked_action_claims']} | 0 |")
    out.append(f"| First agent audio, server (end of user input → first audio chunk) p50 / p90 / p95 | "
               f"{_fmt(fs['p50'])} / {_fmt(fs['p90'])} / {_fmt(fs['p95'])} ms (n={fs['n']}) | p50 < 1500, p95 < 3000 (NF-01) |")
    out.append(f"| First agent audio, client (text sent → first audio byte received) p50 / p90 / p95 | "
               f"{_fmt(fc['p50'])} / {_fmt(fc['p90'])} / {_fmt(fc['p95'])} ms (n={fc['n']}) | |")
    ft, fk = agg["first_audio_tool_turns_ms"], agg["first_audio_talk_turns_ms"]
    out.append(f"| First agent audio, turns with a tool call / talk-only turns, p50 (p90) | "
               f"{_fmt(ft['p50'])} ({_fmt(ft['p90'])}) ms, n={ft['n']} / {_fmt(fk['p50'])} ({_fmt(fk['p90'])}) ms, n={fk['n']} | |")
    out.append(f"| Interventions per run (corrections + cancels, as counted by the server) | "
               f"{agg['interventions_per_run']} | |")
    utter = sum(r["utterances"] for r in results)
    long_ = sum(r["utterances_over_25_words"] for r in results)
    out.append(f"| Agent transcript segments over 25 words (a segment can hold the sentence before and the "
               f"sentence after a tool call) | {long_} of {utter} | ≤ 25 words typical (NF-05) |")
    out.append("")
    out.append("## Per scenario")
    out.append("")
    out.append("| ID | Title | Runs | Passed | Completion | First audio p50 / p90 (ms) | Tool calls / run | Turns | Duration p50 (s) | Interventions / run |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for sid, rows in sorted(by_sc.items()):
        scored = [r for r in rows if not r["infra"]]
        fa = [t["first_audio_ms_server"] for r in rows for t in r["transcript"]
              if t.get("first_audio_ms_server") is not None and t["kind"] != "silence"]
        rate = f"{100 * sum(r['passed'] for r in scored) / len(scored):.0f}%" if scored else "–"
        infra = f" (+{len(rows) - len(scored)} infra)" if len(rows) != len(scored) else ""
        out.append(
            f"| {sid} | {rows[0]['title']} | {len(rows)}{infra} | {sum(r['passed'] for r in rows)} | {rate} | "
            f"{_p(fa, 50)} / {_p(fa, 90)} | {sum(r['tool_calls'] for r in rows) / len(rows):.1f} | "
            f"{rows[0]['turns']} | {_p([r['duration_s'] for r in rows], 50)} | "
            f"{sum(r['interventions'] for r in rows) / len(rows):.1f} |"
        )
    out.append("")
    out.append("## Tool latency (browser actions on the shared demo)")
    out.append("")
    out.append("| Tool | Calls | OK rate | p50 (ms) | p90 (ms) |")
    out.append("|---|---|---|---|---|")
    for name, s in agg["tools"].items():
        out.append(f"| {name} | {s['calls']} | {s['ok_rate']}% | {_fmt(s['p50_ms'])} | {_fmt(s['p90_ms'])} |")
    out.append("")

    barge = [t for r in results for t in r["transcript"] if t["kind"] == "barge_in"]
    if barge:
        stops = [t["barge_in_stop_ms"] for t in barge if t.get("barge_in_stop_ms") is not None]
        out.append(f"Barge-in (typed, sent while the agent was answering): `interrupted` received in "
                   f"{sum(1 for t in barge if t['interrupted'])} of {len(barge)} turns; time to `interrupted` "
                   f"p50 {_p(stops, 50)} ms. Text barge-in is a proxy: real voice barge-in latency depends on "
                   f"Gemini's VAD and is not measured here.")
        out.append("")

    if baseline:
        out.extend(baseline_section(results, baseline, baseline_label))

    failed = [r for r in results if not r["passed"]]
    out.append("## Failures")
    out.append("")
    if not failed:
        out.append("None.")
    cats: dict[str, int] = {}
    for r in failed:
        cats[r["category"] or "?"] = cats.get(r["category"] or "?", 0) + 1
    if failed:
        out.append("By root cause: " + ", ".join(f"{k}: {v}" for k, v in sorted(cats.items())) + ".")
        out.append("Categories: *misunderstood user* (wrong value/intent taken from the user's words), "
                   "*wrong action* (wrong or missing tool call), *misread screen* (agent's statement contradicts "
                   "what the tools observed, incl. false success), *infra* (demo slow, API/quota error).")
        out.append("")
    for r in failed:
        out.append(f"### {r['scenario']} run {r['run']} ({r['rid']}): {r['category']}")
        out.append("")
        for f in r["infra_failures"] + r["hard_failures"]:
            out.append(f"- {f}")
        out.append("")
        out.append("```")
        out.append(excerpt(r))
        out.append("```")
        out.append("")

    soft_counts: dict[str, int] = {}
    for r in results:
        for w in r["soft_warnings"]:
            key = re.sub(r"\(got .*\)", "", re.sub(r"'[^']*'", "…", w)).strip()
            key = f"{r['scenario']} {key}"
            soft_counts[key] = soft_counts.get(key, 0) + 1
    if soft_counts:
        out.append("## Soft warnings (do not fail a run)")
        out.append("")
        for k, v in sorted(soft_counts.items()):
            out.append(f"- {k} ×{v}")
        out.append("")

    if NOTES_PATH.exists():
        out.append(NOTES_PATH.read_text().strip())
        out.append("")

    out.append("## How to run")
    out.append("")
    out.append("```")
    out.append("uv run python -m evals.run_evals                              # full catalogue once")
    out.append("uv run python -m evals.run_evals --scenarios S01,S03 --runs 5")
    out.append("uv run python -m evals.run_evals --tags happy_path,correction --runs 3")
    out.append("uv run python -m evals.run_evals --url ws://127.0.0.1:7860/ws  # against a running server")
    out.append("uv run python -m evals.run_evals --report-from evals/results/a.json,evals/results/b.json")
    out.append("```")
    out.append("")
    out.append("Checks: `tools_any` soft; `tools_none`, `must_not_claim_success`, barge-in reaction and the CRM end "
               "state (`success` fields, `expect_count`, `forbid`, no unexpected records) are hard. A run that fails "
               "only because of infrastructure (quota, demo outage) is reported but excluded from the pass rate. "
               "Success claims are detected with a keyword list (saved, created, moved, done, all set, updated "
               "the record) that ignores negated, conditional and question forms.")
    text = "\n".join(out) + "\n"
    REPORT_PATH.write_text(text)
    return text


def _fmt(v: float | None) -> str:
    return "–" if v is None else f"{v:,.0f}"


# ----------------------------------------------------------------------------- main

def select_scenarios(all_sc: list[dict], ids: str | None, tags: str | None) -> list[dict]:
    out = all_sc
    if ids:
        want = [s.strip().upper() for s in ids.split(",") if s.strip()]
        out = [s for s in out if s["id"] in want]
    if tags:
        want_t = {t.strip() for t in tags.split(",") if t.strip()}
        out = [s for s in out if want_t & set(s.get("tags", []))]
    return out


def save_results(results: list[dict], meta: dict, name: str | None = None) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / (name or f"{now_utc().strftime('%Y%m%d-%H%M%S')}.json")
    path.write_text(json.dumps({"meta": meta, "summary": aggregate(results), "results": results},
                               indent=1, ensure_ascii=False, default=str))
    return path


def load_results(paths: list[str]) -> list[dict]:
    out: list[dict] = []
    for p in paths:
        out.extend(rescore(r) for r in json.loads(Path(p).read_text())["results"])
    return out


def is_quota(r: dict) -> bool:
    return any(re.search(r"free-tier|quota|429|resource_exhausted", f, re.I) for f in r["infra_failures"])


async def main_async(args: argparse.Namespace) -> int:
    scenarios = select_scenarios(json.loads(SCENARIOS_PATH.read_text())["scenarios"], args.scenarios, args.tags)
    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 2
    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="evals-"))
    workdir.mkdir(parents=True, exist_ok=True)
    server: ServerProc | None = None
    verifier = CrmVerifier()
    results: list[dict] = []
    out_path: Path | None = None
    meta = {"started": now_utc().isoformat(timespec="seconds"), "runs": args.runs,
            "scenarios": [s["id"] for s in scenarios], "mode": "typed text, mic off",
            "server": "external" if args.url else "local subprocess"}
    try:
        if args.url:
            url = args.url
        else:
            server = ServerProc(workdir)
            print(f"starting server on port {server.port} (log: {server.log_path})", flush=True)
            await asyncio.to_thread(server.start)
            url = server.ws_url
        await verifier.start()
        consecutive_quota = 0
        for run_no in range(1, args.runs + 1):
            for sc in scenarios:
                print(f"--- {sc['id']} run {run_no}: {sc['title']}", flush=True)
                r = await run_scenario(sc, url, verifier, run_no, quiet_s=args.quiet)
                if is_quota(r):
                    consecutive_quota += 1
                    print(f"    quota/rate limit hit; backing off {args.backoff}s", flush=True)
                    if consecutive_quota >= 3:
                        results.append(r)
                        print("    quota exhausted 3 times in a row; stopping", flush=True)
                        raise KeyboardInterrupt
                    await asyncio.sleep(args.backoff)
                else:
                    consecutive_quota = 0
                results.append(r)
                status = "PASS" if r["passed"] else ("INFRA" if r["infra"] else "FAIL")
                print(f"    {status} in {r['duration_s']}s, {r['tool_calls']} tools, "
                      f"fa={[t['first_audio_ms_server'] for t in r['transcript']]}", flush=True)
                for f in r["infra_failures"] + r["hard_failures"]:
                    print(f"      ! {f}", flush=True)
                for w in r["soft_warnings"]:
                    print(f"      ~ {w}", flush=True)
                out_path = save_results(results, meta, out_path.name if out_path else None)
                await asyncio.sleep(args.cooldown)
    except KeyboardInterrupt:
        print("interrupted; writing partial results", flush=True)
    finally:
        await verifier.close()
        if server is not None:
            await asyncio.to_thread(server.stop)
    if results:
        meta["finished"] = now_utc().isoformat(timespec="seconds")
        out_path = save_results(results, meta, out_path.name if out_path else None)
        rel = out_path.relative_to(ROOT).as_posix()
        if not args.no_report:
            write_report(results, [rel])
        agg = aggregate(results)
        print(f"results: {rel}; pass {agg['passed']}/{agg['runs']}, false-success {agg['false_success']}, "
              f"first-audio p50 {_fmt(agg['first_audio_server_ms']['p50'])} ms")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", help="comma-separated ids, e.g. S01,S02")
    ap.add_argument("--tags", help="comma-separated tags; any match selects a scenario")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--url", help="WebSocket URL of a running server (default: start one)")
    ap.add_argument("--workdir", help="where server logs go (default: a temp dir)")
    ap.add_argument("--quiet", type=float, default=QUIET_S, help="seconds of quiet that end an agent turn")
    ap.add_argument("--cooldown", type=float, default=4.0, help="pause between scenarios (s)")
    ap.add_argument("--backoff", type=float, default=90.0, help="pause after a quota error (s)")
    ap.add_argument("--no-report", action="store_true", help="do not regenerate evals/REPORT.md")
    ap.add_argument("--report-from", help="comma-separated result files: merge them and regenerate the report")
    ap.add_argument("--merge-to", help="with --report-from: also write the merged results to this file name")
    ap.add_argument("--baseline", help="with --report-from: result file(s) from before a change, shown as a "
                                       "before/after comparison (and stored in the merged file)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.report_from:
        paths = [p.strip() for p in args.report_from.split(",") if p.strip()]
        results = load_results(paths)
        sources = [Path(p).name for p in paths]
        bpaths = [p.strip() for p in (args.baseline or "").split(",") if p.strip()]
        baseline = load_results(bpaths) if bpaths else None
        blabel = ", ".join(Path(p).name for p in bpaths)
        if args.merge_to:
            out = save_results(results, {"merged_from": sources, "baseline_from": blabel or None,
                                         "baseline": baseline or []}, args.merge_to)
            sources = [out.relative_to(ROOT).as_posix()]
            if baseline:
                blabel = f"baseline runs stored in {sources[0]} under meta.baseline"
        write_report(results, sources, baseline, blabel)
        print(f"report written from {len(results)} runs")
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
