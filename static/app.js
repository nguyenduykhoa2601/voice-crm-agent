// CRM Voice Guide — browser client.
//
// Talks to the server over one WebSocket (/ws):
//   out: binary PCM16 LE mono 16 kHz mic chunks; JSON {type: start|text|mic|stop}
//   in:  binary 0x01 + PCM16 24 kHz agent audio, 0x02 + JPEG live-view frame;
//        JSON status | transcript | interrupted | turn_complete | action | form | metrics | error

const KIND_AUDIO = 0x01;
const KIND_FRAME = 0x02;

const $ = (id) => document.getElementById(id);
const els = {
  status: $("status"),
  statusText: $("status-text"),
  callBtn: $("call-btn"),
  callBtnLabel: $("call-btn-label"),
  micBtn: $("mic-btn"),
  micLabel: $("mic-label"),
  micMeter: $("mic-meter"),
  speaking: $("speaking"),
  speakingText: $("speaking-text"),
  latency: $("latency"),
  transcript: $("transcript"),
  transcriptEmpty: $("transcript-empty"),
  timeline: $("timeline"),
  timelineEmpty: $("timeline-empty"),
  browser: $("browser"),
  viewport: $("viewport"),
  frame: $("frame"),
  liveBadge: $("live-badge"),
  urlText: $("browser-url-text"),
  acting: $("acting"),
  actingText: $("acting-text"),
  record: $("record"),
  recordEntity: $("record-entity"),
  recordFields: $("record-fields"),
  composer: $("composer"),
  textInput: $("text-input"),
  sendBtn: $("send-btn"),
};

const meterBars = Array.from(els.micMeter.children);
const speakingBars = Array.from(els.speaking.querySelectorAll(".speaking-bars i"));

const state = {
  phase: "idle", // idle | connecting | active
  ws: null,
  endedByUser: false,
  audioCtx: null,
  micStream: null,
  micSource: null,
  captureNode: null,
  playerNode: null,
  micOn: false,
  micAvailable: false,
  agentSpeaking: false,
  open: { user: null, agent: null }, // open transcript bubbles
  lastRole: null,
  actions: new Map(), // id -> {el, name, status}
  frame: { decoding: false, pending: null },
};

/* ------------------------------------------------------------------ status */

const STATUS_TEXT = {
  idle: "Ready when you are — press Start call",
  connecting: "Connecting…",
  starting: "Opening the CRM and waking up the agent…",
  ready: "Live — say hello, or ask for something in EspoCRM",
  busy: "All demo slots are in use right now — please try again in a minute",
  ended: "Call ended",
};

function setStatus(kind, message) {
  els.status.dataset.state = kind;
  els.statusText.textContent = message || STATUS_TEXT[kind] || kind;
  els.status.title = els.statusText.textContent;
}

/* ------------------------------------------------------------ call control */

els.callBtn.addEventListener("click", () => {
  if (state.phase === "idle") startCall();
  else endCall(true);
});

function setPhase(phase) {
  state.phase = phase;
  els.callBtn.dataset.state = phase;
  els.callBtnLabel.textContent =
    phase === "idle" ? "Start call" : phase === "connecting" ? "Connecting…" : "End call";
  const live = phase === "active";
  els.textInput.disabled = !live;
  els.sendBtn.disabled = !live;
  els.textInput.placeholder = live
    ? state.micAvailable
      ? "Or type a message…"
      : "Type a message to the agent…"
    : "Start a call, then type or talk…";
  els.micBtn.disabled = !live || !state.micAvailable;
  els.liveBadge.hidden = !live;
  updateMicUi();
}

async function startCall() {
  setPhase("connecting");
  setStatus("connecting");
  state.endedByUser = false;

  // Audio must be created/resumed inside the click handler (autoplay policy).
  try {
    await setupAudio();
  } catch (err) {
    console.warn("Audio output unavailable:", err);
    addSystemNote("Audio playback is unavailable in this browser — you'll see the transcript only.", true);
  }

  // Microphone is optional: without it the typed fallback still works.
  try {
    await setupMic();
    state.micAvailable = true;
    state.micOn = true;
  } catch (err) {
    console.warn("Microphone unavailable:", err);
    state.micAvailable = false;
    state.micOn = false;
    const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
    addSystemNote(
      denied
        ? "Microphone access was blocked. You can still type below, or allow the mic in your browser and start again."
        : "No microphone found. You can still type messages below.",
      true,
    );
  }

  if (state.phase !== "connecting") {
    teardownAudio(); // user cancelled while we were asking for the mic
    return;
  }
  openSocket();
}

function endCall(byUser) {
  if (state.phase === "idle") return;
  state.endedByUser = byUser;
  const ws = state.ws;
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: "stop" })); } catch { /* ignore */ }
    ws.close(1000, "client ended call");
  } else if (ws && ws.readyState === WebSocket.CONNECTING) {
    ws.close();
  }
  state.ws = null;
  teardownAudio();
  setActing(null);
  closeOpenBubbles();
  for (const a of state.actions.values()) {
    if (a.status === "running") updateActionEl(a, { status: "error", summary: "Stopped — call ended" });
  }
  setPhase("idle");
  if (byUser) setStatus("ended", "Call ended — press Start call to go again");
}

/* -------------------------------------------------------------- websocket */

function socketUrl() {
  const url = new URL("ws", window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  url.search = "";
  url.hash = "";
  return url.toString();
}

function openSocket() {
  let ws;
  try {
    ws = new WebSocket(socketUrl());
  } catch (err) {
    setStatus("error", "Could not connect to the server");
    endCall(false);
    return;
  }
  ws.binaryType = "arraybuffer";
  state.ws = ws;

  ws.addEventListener("open", () => {
    if (state.ws !== ws) return;
    ws.send(JSON.stringify({ type: "start" }));
    if (state.micAvailable) ws.send(JSON.stringify({ type: "mic", on: state.micOn }));
    setPhase("active");
    setStatus("starting");
  });

  ws.addEventListener("message", (event) => {
    if (state.ws !== ws) return;
    if (typeof event.data === "string") handleJson(event.data);
    else handleBinary(event.data);
  });

  ws.addEventListener("close", (event) => {
    if (state.ws !== ws) return; // we closed it ourselves
    const wasConnecting = state.phase === "connecting";
    state.ws = null;
    endCall(false);
    if (els.status.dataset.state === "busy") return; // keep the busy message
    if (wasConnecting) {
      setStatus("error", "Couldn't reach the server — please refresh and try again");
    } else if (els.status.dataset.state !== "ended") {
      const reason = event.reason ? ` (${event.reason})` : "";
      setStatus("error", `Connection closed${reason} — press Start call to reconnect`);
      addSystemNote("The connection to the server closed.", true);
    }
  });

  ws.addEventListener("error", () => {
    // A close event always follows; it carries the user-facing message.
  });
}

function send(obj) {
  const ws = state.ws;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function handleBinary(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 2) return;
  const kind = new Uint8Array(buffer, 0, 1)[0];
  if (kind === KIND_AUDIO) {
    // Copy into a fresh, 2-byte aligned buffer and hand it to the player thread.
    const pcm = buffer.slice(1, 1 + ((buffer.byteLength - 1) & ~1));
    if (state.playerNode) state.playerNode.port.postMessage({ type: "pcm", buffer: pcm }, [pcm]);
  } else if (kind === KIND_FRAME) {
    showFrame(new Blob([new Uint8Array(buffer, 1)], { type: "image/jpeg" }));
  }
}

function handleJson(text) {
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    console.warn("Ignoring non-JSON text message");
    return;
  }
  switch (msg.type) {
    case "status":
      onStatus(msg);
      break;
    case "transcript":
      onTranscript(msg);
      break;
    case "interrupted":
      flushPlayback();
      if (state.open.agent) state.open.agent.wrap.classList.add("interrupted");
      closeBubble("agent");
      break;
    case "turn_complete":
      closeBubble("agent");
      break;
    case "action":
      onAction(msg);
      break;
    case "form":
      onForm(msg);
      break;
    case "metrics":
      onMetrics(msg);
      break;
    case "error":
      addSystemNote(msg.message || "Something went wrong.", true);
      flashWarning(msg.message || "Something went wrong");
      break;
    default:
      break;
  }
}

let warnTimer = 0;

// Show a warning in the status line, then fall back to "live" if the call continues.
function flashWarning(message) {
  setStatus("warn", message);
  clearTimeout(warnTimer);
  warnTimer = setTimeout(() => {
    if (state.phase === "active" && els.status.dataset.state === "warn") setStatus("ready");
  }, 6000);
}

function onStatus(msg) {
  const s = msg.state;
  if (s === "ready") setStatus("ready", STATUS_TEXT.ready);
  else if (s === "starting") setStatus("starting", msg.message || STATUS_TEXT.starting);
  else if (s === "busy") {
    setStatus("busy", msg.message || STATUS_TEXT.busy);
  } else if (s === "ended") {
    setStatus("ended", msg.message || "Call ended — press Start call to go again");
    endCall(false); // a server-initiated end is not an error
  } else {
    setStatus("warn", msg.message || String(s));
  }
}

/* ------------------------------------------------------------- transcript */

function hideEmptyTranscript() {
  els.transcriptEmpty.hidden = true;
}

function scrollTranscript() {
  const t = els.transcript;
  // Only autoscroll if the user hasn't scrolled up to read history.
  if (t.scrollHeight - t.scrollTop - t.clientHeight < 120) t.scrollTop = t.scrollHeight;
}

function makeBubble(role, text, partial) {
  hideEmptyTranscript();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}${partial ? " partial" : ""}`;
  const label = document.createElement("div");
  label.className = "msg-role";
  label.textContent = role === "user" ? "You" : "Agent";
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;
  wrap.append(label, bubble);
  // Collapse the role label for consecutive bubbles from the same speaker.
  if (state.lastRole === role) label.hidden = true;
  state.lastRole = role;
  els.transcript.append(wrap);
  scrollTranscript();
  return { wrap, bubble, text };
}

function closeBubble(role) {
  const open = state.open[role];
  if (!open) return;
  open.wrap.classList.remove("partial");
  if (!open.text.trim()) open.wrap.remove();
  state.open[role] = null;
}

function closeOpenBubbles() {
  closeBubble("user");
  closeBubble("agent");
}

// Non-final messages carry deltas that are appended. A final message closes the
// bubble; if its text is the full utterance (it extends what we have), it
// replaces the accumulated text, otherwise it is treated as a last delta.
function onTranscript(msg) {
  const role = msg.role === "user" ? "user" : "agent";
  const other = role === "user" ? "agent" : "user";
  const text = msg.text || "";
  let open = state.open[role];

  if (!open) {
    if (!text.trim() && msg.final) return;
    if (state.open[other]) closeBubble(other);
    open = state.open[role] = makeBubble(role, "", true);
  }

  if (msg.final) {
    if (text) {
      const acc = open.text;
      const full = !acc || text.startsWith(acc.trimStart()) || text.trim() === acc.trim();
      open.text = full ? text : acc + text;
    }
    open.bubble.textContent = open.text.trim();
    closeBubble(role);
  } else {
    open.text += text;
    open.bubble.textContent = open.text.trimStart();
  }
  scrollTranscript();
}

function addSystemNote(text, isError) {
  hideEmptyTranscript();
  const wrap = document.createElement("div");
  wrap.className = `msg system${isError ? " error" : ""}`;
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;
  wrap.append(bubble);
  state.lastRole = null;
  els.transcript.append(wrap);
  scrollTranscript();
}

/* ---------------------------------------------------------- typed message */

els.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = els.textInput.value.trim();
  if (!text || state.phase !== "active") return;
  closeOpenBubbles();
  makeBubble("user", text, false);
  send({ type: "text", text });
  els.textInput.value = "";
});

for (const hint of document.querySelectorAll(".hint")) {
  hint.addEventListener("click", () => {
    els.textInput.value = hint.textContent.trim();
    if (!els.textInput.disabled) els.textInput.focus();
  });
}

/* -------------------------------------------------------- action timeline */

const ICONS = {
  ok: '<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg>',
  error: '<svg viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>',
};

const TOOL_LABELS = {
  get_screen: "Read the screen",
  open_create_form: "Open create form",
  fill_fields: "Fill in fields",
  save: "Save",
  cancel: "Cancel",
  find_records: "Search records",
  open_record: "Open record",
  edit_record: "Edit record",
  set_stage: "Change stage",
};

function humanizeTool(name) {
  if (!name) return "Action";
  if (TOOL_LABELS[name]) return TOOL_LABELS[name];
  const words = String(name).replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function defaultSummary(name, args) {
  if (!args || typeof args !== "object") return "";
  if (name === "fill_fields" && args.fields && typeof args.fields === "object") {
    return Object.entries(args.fields).map(([k, v]) => `${k}: ${v}`).join(", ");
  }
  return Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
}

function onAction(msg) {
  const id = msg.id != null ? String(msg.id) : `a${state.actions.size + 1}`;
  let entry = state.actions.get(id);
  if (!entry) {
    els.timelineEmpty.hidden = true;
    const li = document.createElement("li");
    li.className = "step";
    li.innerHTML =
      '<span class="step-icon"></span>' +
      '<div><div class="step-name"></div><div class="step-summary"></div></div>' +
      '<span class="step-ms"></span>';
    els.timeline.append(li);
    entry = { el: li, name: msg.name, args: msg.args, status: null };
    state.actions.set(id, entry);
  }
  updateActionEl(entry, msg);
  els.timeline.scrollTop = els.timeline.scrollHeight;
  refreshActing();
}

function updateActionEl(entry, msg) {
  const status = msg.status || entry.status || "running";
  entry.status = status;
  if (msg.name) entry.name = msg.name;
  if (msg.args) entry.args = msg.args;
  const el = entry.el;
  el.dataset.status = status;
  el.querySelector(".step-icon").innerHTML =
    status === "running" ? '<span class="spinner" aria-label="Running"></span>' : ICONS[status] || ICONS.ok;
  el.querySelector(".step-name").textContent = humanizeTool(entry.name);
  const summary = msg.summary || entry.summary || defaultSummary(entry.name, entry.args);
  entry.summary = summary;
  el.querySelector(".step-summary").textContent = summary;
  el.querySelector(".step-ms").textContent =
    typeof msg.ms === "number" && status !== "running" ? formatMs(msg.ms) : "";
}

function refreshActing() {
  let running = null;
  for (const a of state.actions.values()) if (a.status === "running") running = a;
  setActing(running);
}

function setActing(action) {
  const on = Boolean(action);
  els.acting.hidden = !on;
  els.browser.classList.toggle("is-acting", on);
  if (on) els.actingText.textContent = `Agent is acting · ${humanizeTool(action.name)}`;
}

function formatMs(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/* ------------------------------------------------------- record snapshot */

function onForm(msg) {
  const fields = msg.fields && typeof msg.fields === "object" ? Object.entries(msg.fields) : [];
  if (msg.url) {
    try {
      const u = new URL(msg.url);
      els.urlText.textContent = u.host + u.pathname + u.hash;
    } catch {
      els.urlText.textContent = msg.url;
    }
  }
  if (!fields.length) {
    els.record.hidden = true;
    return;
  }
  els.recordEntity.textContent = msg.entity || "Record";
  els.recordFields.replaceChildren(
    ...fields.flatMap(([label, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
      return [dt, dd];
    }),
  );
  els.record.hidden = false;
}

/* ---------------------------------------------------------------- metrics */

function onMetrics(msg) {
  if (typeof msg.first_audio_ms === "number") {
    els.latency.textContent = `${formatMs(msg.first_audio_ms)} to voice`;
  }
}

/* -------------------------------------------------------------- live view */

const ctx2d = els.frame.getContext("2d");

function showFrame(blob) {
  // Only ever decode the newest frame; drop any that arrive mid-decode.
  if (state.frame.decoding) {
    state.frame.pending = blob;
    return;
  }
  state.frame.decoding = true;
  decodeFrame(blob);
}

async function decodeFrame(blob) {
  try {
    if (typeof createImageBitmap === "function") {
      const bitmap = await createImageBitmap(blob);
      drawFrame(bitmap, bitmap.width, bitmap.height);
      bitmap.close();
    } else {
      const url = URL.createObjectURL(blob);
      try {
        const img = new Image();
        img.src = url;
        await img.decode();
        drawFrame(img, img.naturalWidth, img.naturalHeight);
      } finally {
        URL.revokeObjectURL(url);
      }
    }
  } catch (err) {
    console.warn("Dropped an undecodable frame", err);
  }
  const next = state.frame.pending;
  state.frame.pending = null;
  if (next) decodeFrame(next);
  else state.frame.decoding = false;
}

function drawFrame(source, w, h) {
  if (els.frame.width !== w || els.frame.height !== h) {
    els.frame.width = w;
    els.frame.height = h;
  }
  ctx2d.drawImage(source, 0, 0, w, h);
  els.viewport.classList.add("has-frame");
}

/* ------------------------------------------------------------------ audio */

async function setupAudio() {
  let created = false;
  if (!state.audioCtx) {
    // Native rate: we resample ourselves in the worklets (16 kHz is not universally supported).
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: "interactive" });
    created = true;
  }
  const ctx = state.audioCtx;
  // Resume synchronously within the click gesture (required by Safari's autoplay policy).
  const resumed = ctx.state === "suspended" ? ctx.resume() : Promise.resolve();
  if (created) {
    state.workletReady = ctx.audioWorklet.addModule(new URL("audio-worklet.js", import.meta.url));
  }
  await state.workletReady;
  await resumed;

  const player = new AudioWorkletNode(ctx, "pcm-player", {
    numberOfInputs: 0,
    numberOfOutputs: 1,
    outputChannelCount: [1],
  });
  player.port.onmessage = (event) => onPlayerLevel(event.data);
  player.connect(ctx.destination);
  state.playerNode = player;
}

async function setupMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    const err = new Error("getUserMedia not supported");
    err.name = "NotFoundError";
    throw err;
  }
  if (!state.audioCtx) throw new Error("Audio context unavailable");
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video: false,
  });
  const ctx = state.audioCtx;
  const source = ctx.createMediaStreamSource(stream);
  const capture = new AudioWorkletNode(ctx, "pcm-capture", {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    channelCount: 1,
    channelCountMode: "explicit",
    channelInterpretation: "speakers",
  });
  capture.port.onmessage = (event) => onMicChunk(event.data);
  // Keep the node in the rendering graph with a silent sink.
  const sink = ctx.createGain();
  sink.gain.value = 0;
  source.connect(capture);
  capture.connect(sink);
  sink.connect(ctx.destination);

  state.micStream = stream;
  state.micSource = source;
  state.captureNode = capture;
  state.micSink = sink;

  for (const track of stream.getAudioTracks()) {
    track.addEventListener("ended", () => {
      if (state.phase === "idle") return;
      state.micAvailable = false;
      setMic(false);
      addSystemNote("The microphone was disconnected. You can keep typing.", true);
      setPhase(state.phase);
    });
  }
}

function teardownAudio() {
  if (state.micStream) for (const t of state.micStream.getTracks()) t.stop();
  for (const node of [state.micSource, state.captureNode, state.micSink, state.playerNode]) {
    if (node) {
      try { node.disconnect(); } catch { /* ignore */ }
      if (node.port) node.port.onmessage = null;
    }
  }
  if (state.playerNode) state.playerNode.port.postMessage({ type: "flush" });
  state.micStream = state.micSource = state.captureNode = state.micSink = state.playerNode = null;
  state.micOn = false;
  state.micAvailable = false;
  // Keep the AudioContext around (suspended) so a second call starts instantly.
  if (state.audioCtx && state.audioCtx.state === "running") state.audioCtx.suspend().catch(() => {});
  renderMeter(0);
  onPlayerLevel({ rms: 0, bufferedMs: 0, playing: false });
}

function onMicChunk(data) {
  if (data.type !== "chunk") return;
  const live = state.micOn && state.phase === "active";
  renderMeter(live ? data.rms : 0);
  if (!live) return;
  const ws = state.ws;
  // Skip sending if the socket is badly backed up (slow network) rather than build latency.
  if (ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 64 * 1024) ws.send(data.buffer);
}

function flushPlayback() {
  if (state.playerNode) state.playerNode.port.postMessage({ type: "flush" });
}

/* --------------------------------------------------------------- mic UI */

els.micBtn.addEventListener("click", () => setMic(!state.micOn));

function setMic(on) {
  state.micOn = on && state.micAvailable;
  if (state.micStream) for (const t of state.micStream.getAudioTracks()) t.enabled = state.micOn;
  send({ type: "mic", on: state.micOn });
  updateMicUi();
}

function updateMicUi() {
  const live = state.phase === "active" && state.micAvailable;
  const on = live && state.micOn;
  els.micBtn.setAttribute("aria-pressed", String(on || !live));
  els.micBtn.classList.toggle("is-live", on);
  els.micBtn.classList.toggle("is-muted", live && !state.micOn);
  els.micLabel.textContent = !live ? (state.phase === "active" ? "No mic" : "Mic off") : on ? "Mic on" : "Muted";
  els.micBtn.title = on ? "Mute microphone" : "Unmute microphone";
  if (!on) renderMeter(0);
}

// Map RMS (0..1) to a perceptual 0..1 level via dBFS (-60 dB .. -10 dB).
function levelFromRms(rms) {
  if (rms <= 0) return 0;
  const db = 20 * Math.log10(rms);
  return Math.min(1, Math.max(0, (db + 60) / 50));
}

function renderMeter(rms) {
  const lit = Math.round(levelFromRms(rms) * meterBars.length);
  meterBars.forEach((bar, i) => {
    bar.classList.toggle("on", i < lit);
    bar.classList.toggle("hot", i >= meterBars.length - 2);
  });
}

/* --------------------------------------------------- agent speaking state */

let quietSince = 0;

function onPlayerLevel(data) {
  if (data.type && data.type !== "level") return;
  const active = data.playing || data.bufferedMs > 5;
  const now = performance.now();
  if (active) quietSince = 0;
  else if (!quietSince) quietSince = now;
  // Short hold so the indicator doesn't flicker between words; a flush (barge-in) is instant.
  const speaking = !data.flushed && (active || (state.agentSpeaking && now - quietSince < 300));
  if (speaking !== state.agentSpeaking) {
    state.agentSpeaking = speaking;
    els.speaking.dataset.active = String(speaking);
    els.speakingText.textContent = speaking ? "Agent is speaking" : "Agent is quiet";
  }
  const level = speaking ? levelFromRms(data.rms) : 0;
  speakingBars.forEach((bar, i) => {
    const shape = [0.55, 0.85, 1, 0.75, 0.5][i];
    bar.style.height = `${4 + Math.round(level * shape * 12)}px`;
  });
}

window.addEventListener("beforeunload", () => {
  if (state.phase !== "idle") endCall(true);
});

setPhase("idle");
