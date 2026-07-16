import { Timeline } from "./timeline.js";

const ENGINE_COLORS = {
  unimrcp_vad: "#ffa94d",
  silero_vad: "#69db7c",
  ten_vad: "#da77f2",
};
const FALLBACK_COLORS = ["#4dabf7", "#f783ac", "#a9e34b", "#ffd43b"];

const state = {
  engines: [],
  enhancers: [],
  currentSession: null, // session id being viewed
  liveSession: null,
  allSessions: [], // every session from the server, unfiltered
  sessionFilterDate: null, // "YYYY-MM-DD" day filter, or null for all days
};

const timeline = new Timeline(document.getElementById("timeline"));
window.vadTimeline = timeline; // console/debug access
const audio = document.getElementById("player");
const els = {
  sessionList: document.getElementById("sessionList"),
  sessionDate: document.getElementById("sessionDate"),
  sessionDateClear: document.getElementById("sessionDateClear"),
  liveBadge: document.getElementById("liveBadge"),
  title: document.getElementById("sessionTitle"),
  followBtn: document.getElementById("followBtn"),
  fitBtn: document.getElementById("fitBtn"),
  reanalyzeBtn: document.getElementById("reanalyzeBtn"),
  enginePanel: document.getElementById("enginePanel"),
  enhancerPanel: document.getElementById("enhancerPanel"),
  recordBtn: document.getElementById("recordBtn"),
  wavBtn: document.getElementById("wavBtn"),
  wavFileInput: document.getElementById("wavFileInput"),
  imprintSel: document.getElementById("imprintSel"),
  recHint: document.getElementById("recHint"),
  recLevelFill: document.getElementById("recLevelFill"),
};

function colorOf(name) {
  if (!ENGINE_COLORS[name]) {
    ENGINE_COLORS[name] = FALLBACK_COLORS[Object.keys(ENGINE_COLORS).length % FALLBACK_COLORS.length];
  }
  return ENGINE_COLORS[name];
}

// one-line "what is this" tooltip for each engine/enhancer card header
const ENGINE_DESC = {
  unimrcp_vad: "UniMRCP'nin yerleşik enerji dedektörü (mpf_activity_detector.c) — konuşmayı yalnızca frame genliğinden işaretler. Grafikteki eğri her 10 ms frame'in ortalama |örnek| genliğidir (log ölçek); genlik 'eşik' çizgisini aşınca konuşma sayılır.",
  silero_vad: "Silero nöral VAD (ONNX) — frame başına konuşma olasılığı üretir. Grafik: 0..1 olasılık; 'threshold' çizgisini geçen frame'ler konuşma adayıdır.",
  ten_vad: "TEN framework nöral VAD — frame başına konuşma olasılığı üretir. Grafik: 0..1 olasılık; 'threshold' çizgisini geçen frame'ler konuşma adayıdır.",
  arf_vad: "Adaptif SNR dedektörü (arf plugin) + WebRTC spektral kapı füzyonu. Grafikteki eğri sesin adaptif gürültü tabanının kaç dB üstünde olduğudur (SNR): 'başlar' çizgisini geçince konuşma başlar, 'biter' çizgisinin altına inince biter. SNR tek karar değildir — spektral (fvad) ve yakınlık kapıları da vetolayabilir; gerçek sonucu segment çubukları gösterir.",
  arf_enhance: "Recognizer (STT) sesini temizler: denoise, de-boom, de-muffle, level, limit. VAD engine'lerini etkilemez.",
};

/* ---------- tooltips ---------- */
// Custom hover tooltip with a 200 ms delay. Native title tooltips fire on a
// fixed ~1 s OS delay that can't be tuned, so we drive our own from data-tip
// attributes and a single floating element (also avoids sidebar clipping).
const TIP_DELAY_MS = 200;
const tipEl = document.createElement("div");
tipEl.id = "tooltip";
document.body.appendChild(tipEl);
let tipTimer = null;
let tipTarget = null;

function positionTip(el) {
  const r = el.getBoundingClientRect();
  const tr = tipEl.getBoundingClientRect();
  const pad = 8;
  let left = r.left;
  if (left + tr.width > window.innerWidth - pad) left = window.innerWidth - pad - tr.width;
  if (left < pad) left = pad;
  let top = r.bottom + 6;
  if (top + tr.height > window.innerHeight - pad) top = r.top - tr.height - 6; // flip above
  tipEl.style.left = Math.round(left) + "px";
  tipEl.style.top = Math.round(top) + "px";
}

document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-tip]");
  if (!el || el === tipTarget) return;
  tipTarget = el;
  clearTimeout(tipTimer);
  tipTimer = setTimeout(() => {
    tipEl.textContent = el.dataset.tip;
    positionTip(el);
    tipEl.classList.add("show");
  }, TIP_DELAY_MS);
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest("[data-tip]");
  if (!el || (e.relatedTarget && el.contains(e.relatedTarget))) return;
  tipTarget = null;
  clearTimeout(tipTimer);
  tipEl.classList.remove("show");
});

/* ---------- sessions ---------- */

async function refreshSessions() {
  state.allSessions = await (await fetch("/api/sessions")).json();
  renderSessionList();
}

// Local YYYY-MM-DD for a session, matching the <input type="date"> value format
// so the calendar day filter can compare against it directly.
function sessionDay(s) {
  if (!s.started_at) return null;
  const d = new Date(s.started_at * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function renderSessionList() {
  const day = state.sessionFilterDate;
  const sessions = (state.allSessions || []).filter((s) => !day || sessionDay(s) === day);
  els.sessionList.innerHTML = "";
  if (!sessions.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = day ? "no recordings on this day" : "no recordings yet";
    els.sessionList.appendChild(li);
    return;
  }
  for (const s of sessions) {
    const li = document.createElement("li");
    const when = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : s.id;
    li.innerHTML = `${when}
      <span class="meta">${((s.duration_ms || 0) / 1000).toFixed(1)}s - ${(s.engines || []).join(", ")}</span>`;
    li.dataset.id = s.id;
    if (s.id === state.currentSession) li.classList.add("selected");
    li.onclick = () => openSession(s.id);
    els.sessionList.appendChild(li);
  }
}

async function openSession(id) {
  const res = await fetch(`/api/sessions/${id}`);
  if (!res.ok) return;
  renderSession(await res.json());
}

// Draw a session payload. preserveView keeps the current zoom/pan (used by
// re-analyze so tuning doesn't reset the graph the user is inspecting).
function renderSession(session, { preserveView = false } = {}) {
  const view = { ...timeline.view };
  state.currentSession = session.id;
  const lanes = Object.entries(session.engines).map(([name, r]) => ({
    name,
    color: colorOf(name),
    axis: r.axis,
    points: gridToPoints(r.scores),
    segments: r.segments.map((s) => ({ ...s })),
    events: (r.events || []).filter((e) => e.kind === "noinput").map((e) => ({ kind: e.kind, at: e.at_ms })),
  }));
  timeline.setModel({
    duration: session.duration_ms,
    peaks: { dt: session.peaks.dt_ms, values: session.peaks.values },
    lanes,
    live: false,
  });
  if (preserveView) {
    timeline.view = view;
    timeline.requestRender();
  }
  els.title.textContent = `${session.id} (${(session.duration_ms / 1000).toFixed(1)}s)`;
  // (Re)load the player only when the source actually changes: switching to a
  // different recording, or returning from a live view (audio was hidden and
  // pointed at the previous clip). Re-analyze of the same open recording keeps
  // the same URL, so we don't reassign audio.src mid-playback — doing so aborts
  // the current play() with a console error and strands the player's position.
  const audioUrl = `/api/sessions/${session.id}/audio.wav`;
  if (!audio.src.endsWith(audioUrl)) {
    audio.pause();
    audio.src = audioUrl;
    audio.load();
  }
  audio.style.display = "";
  els.reanalyzeBtn.style.display = "";
  renderEnginePanel(); // a recording is open now: card buttons become "Re-analyze recording"
  renderEnhancerPanel();
  refreshSessions();
}

function gridToPoints(scores) {
  if (!scores) return [];
  return scores.values.map((v, i) => [scores.t0_ms + i * scores.dt_ms, v]);
}

/* ---------- live updates ---------- */

function startLiveView(sessionId) {
  state.currentSession = sessionId;
  timeline.setModel({ duration: 0, peaks: { dt: 10, values: [] }, lanes: [], live: true });
  timeline.view.msPerPx = 20;
  timeline.follow = true;
  els.title.textContent = `${sessionId} - LIVE`;
  audio.style.display = "none";
  els.reanalyzeBtn.style.display = "none"; // no offline re-run during a live call
  renderEnginePanel(); // live: card buttons revert to "Apply (next call)"
  renderEnhancerPanel();
}

function handleMessage(msg) {
  switch (msg.kind) {
    case "call_state":
      if (msg.state === "active") {
        state.liveSession = msg.session_id;
        els.liveBadge.textContent = "LIVE";
        els.liveBadge.classList.add("active");
        startLiveView(msg.session_id);
      } else if (msg.state === "finished") {
        state.liveSession = null;
        els.liveBadge.textContent = "idle";
        els.liveBadge.classList.remove("active");
        if (state.currentSession === msg.session_id) openSession(msg.session_id);
        else refreshSessions();
        // clear the "processing…" hint once the finished recording is in
        if (recorder.mode === "browser" && !browserRec.recording)
          setRecHint("one click: speak into the mic, click again to stop — all engines run live", false);
      }
      break;
    case "audio_peaks":
      if (msg.session_id === state.currentSession) timeline.appendPeaks(msg.t0_ms, msg.dt_ms, msg.peaks);
      break;
    case "scores":
      if (msg.session_id === state.currentSession) {
        timeline.appendScores(msg.engine, colorOf(msg.engine), msg.points);
        const lane = timeline.laneByName(msg.engine);
        if (lane && !lane.axis) lane.axis = state.engines.find((e) => e.name === msg.engine)?.axis;
      }
      break;
    case "segment":
      if (msg.session_id === state.currentSession)
        timeline.upsertSegment(msg.engine, colorOf(msg.engine), msg.index, {
          start_ms: msg.start_ms,
          end_ms: msg.end_ms,
          final: msg.final,
        });
      break;
    case "event":
      if (msg.session_id === state.currentSession)
        timeline.addEvent(msg.engine, colorOf(msg.engine), msg.event, msg.at_ms);
      break;
  }
}

function connectWs() {
  // wss:// on an HTTPS page — a secure page can't open an insecure ws:// socket
  // (mixed content), so the live hub was silently blocked on the deployed site.
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connectWs, 1500);
}

/* ---------- one-button recorder (drives the softphone client) ---------- */

const recorder = { running: false, state: "idle", busy: false, mode: "softphone" };

async function readErrorDetail(res) {
  try {
    const data = await res.json();
    const detail = data.detail ?? data;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    return JSON.stringify(detail);
  } catch {
    return `request failed (HTTP ${res.status})`;
  }
}

function renderRecorder(status) {
  const btn = els.recordBtn;
  if (!recorder.running) {
    btn.disabled = true;
    els.wavBtn.disabled = true;
    btn.classList.remove("recording");
    btn.innerHTML = "&#127908; Record";
    setRecHint("softphone client starting… (if this persists, run `make run`)", false);
    els.recLevelFill.style.width = "0";
    return;
  }
  btn.disabled = recorder.busy;
  els.wavBtn.disabled = recorder.busy || recorder.state !== "idle";
  if (recorder.state === "idle") {
    btn.classList.remove("recording");
    btn.innerHTML = "&#127908; Record";
    if (status?.error) setRecHint(status.error, true);
    else setRecHint("one click: speak into the mic, click again to stop — all engines run live", false);
    els.recLevelFill.style.width = "0";
  } else {
    btn.classList.add("recording");
    btn.innerHTML = "&#9632; Stop";
    setRecHint("recording — speak now; Stop opens the results", false);
    els.recLevelFill.style.width = Math.min(100, (status?.level || 0) * 300) + "%";
  }
}

function setRecHint(text, isError) {
  els.recHint.textContent = text;
  els.recHint.classList.toggle("error", !!isError);
}

async function pollSoftphone() {
  let running = false;
  try {
    const data = await (await fetch("/api/softphone")).json();
    running = !!data.running;
    if (running) {
      recorder.mode = "softphone";
      recorder.running = true;
      recorder.state = data.status?.state || "idle";
      renderRecorder(data.status);
    }
  } catch {
    /* no softphone client reachable — fall back to browser-mic mode below */
  }
  if (!running) enterBrowserMode();
  setTimeout(pollSoftphone, recorder.state === "idle" ? 2000 : 300);
}

// No local softphone client (e.g. the hosted public app): record straight from
// the browser's microphone instead. The button is always available in this mode.
function enterBrowserMode() {
  recorder.mode = "browser";
  recorder.running = true;
  if (!browserRec.recording) {
    recorder.state = "idle";
    renderRecorder(null);
  }
}

/* ---------- browser-microphone capture (getUserMedia -> 8 kHz PCM -> /api/record) ---------- */

const browserRec = { ws: null, ctx: null, stream: null, node: null, recording: false };

// Runs on the audio thread: convert each float frame to int16 and hand it back.
const CAPTURE_WORKLET = `
class PCMCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) {
      const pcm = new Int16Array(ch.length);
      for (let i = 0; i < ch.length; i++) {
        const s = Math.max(-1, Math.min(1, ch[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm-capture', PCMCapture);
`;

async function startBrowserRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: false, autoGainControl: false },
  });
  // Forcing the context to 8 kHz makes the browser resample the mic for us.
  const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 8000 });
  const url = URL.createObjectURL(new Blob([CAPTURE_WORKLET], { type: "application/javascript" }));
  try {
    await ctx.audioWorklet.addModule(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const law = els.imprintSel && els.imprintSel.value;
  const q = law ? `?imprint=${law}` : "";
  const ws = new WebSocket(`${proto}://${location.host}/api/record${q}`);
  ws.binaryType = "arraybuffer";
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error("recording connection failed"));
  });
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm-capture");
  node.port.onmessage = (e) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(e.data);
    const a = new Int16Array(e.data);
    let peak = 0;
    for (let i = 0; i < a.length; i++) {
      const v = Math.abs(a[i]);
      if (v > peak) peak = v;
    }
    els.recLevelFill.style.width = Math.min(100, (peak / 32768) * 300) + "%";
  };
  src.connect(node);
  node.connect(ctx.destination); // keep the graph pulling; the node emits silence
  Object.assign(browserRec, { ws, ctx, stream, node, recording: true });
}

async function stopBrowserRecording() {
  const { ws, ctx, stream, node } = browserRec;
  browserRec.recording = false;
  try { node && node.disconnect(); } catch {}
  try { stream && stream.getTracks().forEach((t) => t.stop()); } catch {}
  try { if (ws && ws.readyState === WebSocket.OPEN) ws.send("stop"); } catch {}
  try { ctx && (await ctx.close()); } catch {}
  try { ws && ws.close(); } catch {}
  Object.assign(browserRec, { ws: null, ctx: null, stream: null, node: null });
  els.recLevelFill.style.width = "0";
}

async function toggleBrowserRecording() {
  if (recorder.busy) return;
  recorder.busy = true;
  els.recordBtn.disabled = true;
  try {
    if (!browserRec.recording) {
      setRecHint("starting microphone…", false);
      await startBrowserRecording();
      recorder.state = "active";
      els.recordBtn.classList.add("recording");
      els.recordBtn.innerHTML = "&#9632; Stop";
      setRecHint("recording — speak now; Stop opens the results", false);
    } else {
      await stopBrowserRecording();
      recorder.state = "idle";
      els.recordBtn.classList.remove("recording");
      els.recordBtn.innerHTML = "&#127908; Record";
      // the server is finalizing (writing the WAV + last engine frames); the hub's
      // call_state 'finished' clears this and opens the result
      setRecHint("processing — finishing the recording…", false);
    }
  } catch (err) {
    await stopBrowserRecording().catch(() => {});
    recorder.state = "idle";
    els.recordBtn.classList.remove("recording");
    els.recordBtn.innerHTML = "&#127908; Record";
    setRecHint(`microphone error: ${err.message || err} — allow mic access and retry`, true);
  } finally {
    recorder.busy = false;
    els.recordBtn.disabled = false;
  }
}

els.recordBtn.onclick = async () => {
  if (recorder.busy) return;
  if (recorder.mode === "browser") return toggleBrowserRecording();
  recorder.busy = true;
  els.recordBtn.disabled = true;
  try {
    if (recorder.state === "idle") {
      setRecHint("starting the call…", false);
      const res = await fetch("/api/softphone/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "mic" }),
      });
      if (!res.ok) setRecHint(await readErrorDetail(res), true);
      else recorder.state = "active";
    } else {
      await fetch("/api/softphone/stop", { method: "POST" });
      recorder.state = "idle";
    }
  } catch (err) {
    setRecHint(`request failed: ${err.message || err}`, true);
  } finally {
    recorder.busy = false;
    els.recordBtn.disabled = !recorder.running;
  }
};

els.wavBtn.onclick = () => {
  // open the OS file dialog (Finder); the actual work happens on change
  if (recorder.busy || recorder.state !== "idle") return;
  els.wavFileInput.click();
};

els.wavFileInput.onchange = async () => {
  const file = els.wavFileInput.files[0];
  els.wavFileInput.value = ""; // let the same file be picked again later
  if (!file || recorder.busy || recorder.state !== "idle") return;
  recorder.busy = true;
  els.wavBtn.disabled = true;
  try {
    setRecHint(`processing — uploading & analyzing ${file.name}…`, false);
    const fd = new FormData();
    fd.append("file", file);
    // browser mode has no softphone client: analyze the WAV headless instead
    const law = els.imprintSel && els.imprintSel.value;
    let endpoint = recorder.mode === "browser" ? "/api/record/upload" : "/api/softphone/upload";
    if (recorder.mode === "browser" && law) endpoint += `?imprint=${law}`;
    const res = await fetch(endpoint, { method: "POST", body: fd });
    if (!res.ok) {
      setRecHint(await readErrorDetail(res), true);
    } else if (recorder.mode === "browser") {
      const { session_id } = await res.json();
      setRecHint("one click: speak into the mic, click again to stop — all engines run live", false);
      if (session_id) openSession(session_id);
    } else {
      recorder.state = "active"; // the file plays out, then the call ends on its own
    }
  } catch (err) {
    setRecHint(`upload failed: ${err.message || err}`, true);
  } finally {
    recorder.busy = false;
  }
};

/* ---------- engine panel ---------- */

async function refreshEngines() {
  state.engines = await (await fetch("/api/engines")).json();
  renderEnginePanel();
}

function renderEnginePanel() {
  els.enginePanel.innerHTML = "";
  for (const engine of state.engines) {
    const card = document.createElement("div");
    card.className = "engineCard" + (engine.available ? "" : " unavailable");
    const head = document.createElement("div");
    head.className = "head";
    head.innerHTML = `<span class="swatch" style="background:${colorOf(engine.name)}"></span>
      <span class="name">${engine.display_name}</span>`;
    if (ENGINE_DESC[engine.name]) head.dataset.tip = ENGINE_DESC[engine.name];
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = engine.enabled;
    toggle.disabled = !engine.available;
    toggle.dataset.tip = `${engine.display_name} motorunu sonraki çağrı ve re-analysis için etkinleştir`;
    toggle.onchange = () => putEngine(engine.name, { enabled: toggle.checked });
    head.appendChild(toggle);
    card.appendChild(head);
    if (!engine.available) {
      const reason = document.createElement("div");
      reason.className = "reason";
      reason.textContent = engine.reason;
      card.appendChild(reason);
    } else if (engine.params.length) {
      const grid = document.createElement("div");
      grid.className = "params";
      const inputs = {};
      for (const spec of engine.params) {
        const label = document.createElement("label");
        label.textContent = spec.unit ? `${spec.label} (${spec.unit})` : spec.label;
        const input = document.createElement("input");
        if (spec.type === "bool") {
          input.type = "checkbox";
          input.checked = Boolean(engine.values[spec.name]);
        } else {
          input.type = "number";
          if (spec.min != null) input.min = spec.min;
          if (spec.max != null) input.max = spec.max;
          if (spec.step != null) input.step = spec.step;
          input.value = engine.values[spec.name];
        }
        if (spec.help) label.dataset.tip = input.dataset.tip = spec.help;
        inputs[spec.name] = input;
        grid.append(label, input);
      }
      card.appendChild(grid);
      const apply = document.createElement("button");
      apply.className = "apply";
      // with a recorded session open, tuning re-runs it offline immediately;
      // otherwise the params just wait for the next live call
      apply.textContent = hasRecordedSession() ? "Re-analyze recording" : "Apply (next call)";
      apply.dataset.tip = hasRecordedSession()
        ? "Bu engine'i açık kayıt üzerinde bu parametrelerle yeniden çalıştır."
        : "Bu parametreleri kaydet; sonraki canlı çağrıda geçerli olur.";
      apply.onclick = () => {
        const params = {};
        for (const [name, input] of Object.entries(inputs))
          params[name] = input.type === "checkbox" ? input.checked : Number(input.value);
        applyOrReanalyze(engine.name, params, apply);
      };
      card.appendChild(apply);
    }
    els.enginePanel.appendChild(card);
  }
}

function hasRecordedSession() {
  return Boolean(state.currentSession) && !timeline.live;
}

async function applyOrReanalyze(name, params, btn) {
  btn.disabled = true;
  try {
    const res = await fetch(`/api/engines/${name}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params }),
    });
    if (res.ok) state.engines = await res.json();
    if (hasRecordedSession()) {
      btn.textContent = "Analyzing…";
      const r = await fetch(`/api/sessions/${state.currentSession}/reanalyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engines: [name] }),
      });
      if (r.ok) renderSession(await r.json(), { preserveView: true });
      else btn.textContent = await readErrorDetail(r);
    }
  } catch (err) {
    btn.textContent = `failed: ${err.message || err}`;
  } finally {
    renderEnginePanel();
  }
}

async function reanalyzeAll() {
  if (!hasRecordedSession()) return;
  els.reanalyzeBtn.disabled = true;
  els.reanalyzeBtn.textContent = "Analyzing…";
  try {
    const r = await fetch(`/api/sessions/${state.currentSession}/reanalyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engines: null }), // all enabled engines
    });
    if (r.ok) renderSession(await r.json(), { preserveView: true });
  } finally {
    els.reanalyzeBtn.disabled = false;
    els.reanalyzeBtn.textContent = "Re-analyze all";
  }
}

async function putEngine(name, body) {
  const res = await fetch(`/api/engines/${name}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    state.engines = await res.json();
    renderEnginePanel();
  }
}

/* ---------- enhancer panel (recognizer/STT audio preview) ---------- */

const ENHANCER_COLOR = "#4dd4c4";

async function refreshEnhancers() {
  state.enhancers = await (await fetch("/api/enhancers")).json();
  renderEnhancerPanel();
}

function renderEnhancerPanel() {
  els.enhancerPanel.innerHTML = "";
  for (const enh of state.enhancers) {
    const card = document.createElement("div");
    card.className = "engineCard" + (enh.available ? "" : " unavailable");
    const head = document.createElement("div");
    head.className = "head";
    head.innerHTML = `<span class="swatch" style="background:${ENHANCER_COLOR}"></span>
      <span class="name">${enh.display_name}</span>`;
    if (ENGINE_DESC[enh.name]) head.dataset.tip = ENGINE_DESC[enh.name];
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = enh.enabled;
    toggle.disabled = !enh.available;
    toggle.dataset.tip = `${enh.display_name} enhancer'ını recognizer'a (STT) giden sese uygula`;
    toggle.onchange = () => putEnhancer(enh.name, { enabled: toggle.checked });
    head.appendChild(toggle);
    card.appendChild(head);
    if (!enh.available) {
      const reason = document.createElement("div");
      reason.className = "reason";
      reason.textContent = enh.reason;
      card.appendChild(reason);
    } else if (enh.params.length) {
      const grid = document.createElement("div");
      grid.className = "params";
      const inputs = {};
      for (const spec of enh.params) {
        const label = document.createElement("label");
        label.textContent = spec.unit ? `${spec.label} (${spec.unit})` : spec.label;
        const input = document.createElement("input");
        if (spec.type === "bool") {
          input.type = "checkbox";
          input.checked = Boolean(enh.values[spec.name]);
        } else {
          input.type = "number";
          if (spec.min != null) input.min = spec.min;
          if (spec.max != null) input.max = spec.max;
          if (spec.step != null) input.step = spec.step;
          input.value = enh.values[spec.name];
        }
        if (spec.help) label.dataset.tip = input.dataset.tip = spec.help;
        inputs[spec.name] = input;
        grid.append(label, input);
      }
      card.appendChild(grid);
      const apply = document.createElement("button");
      apply.className = "apply";
      // Applying stores the params and, with a recording open, plays the
      // enhanced audio (what the recognizer/STT would hear). The enhancer does
      // not feed the VAD engines, so this never re-runs detection.
      apply.textContent = hasRecordedSession() ? "Apply & play enhanced" : "Apply";
      apply.dataset.tip = hasRecordedSession()
        ? "Bu ayarları uygula ve recognizer'ın duyacağı iyileştirilmiş sesi çal."
        : "Bu enhancer ayarlarını sonraki çağrı için kaydet.";
      apply.onclick = async () => {
        const params = {};
        for (const [name, input] of Object.entries(inputs))
          params[name] = input.type === "checkbox" ? input.checked : Number(input.value);
        await putEnhancer(enh.name, { params });
        playEnhanced();
      };
      card.appendChild(apply);
    }
    els.enhancerPanel.appendChild(card);
  }
}

async function putEnhancer(name, body) {
  const res = await fetch(`/api/enhancers/${name}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    state.enhancers = await res.json();
    renderEnhancerPanel();
    // The enhancer feeds the recognizer (STT) preview only, not the VAD
    // engines, so changing it does NOT re-run detection. Use "Apply & play
    // enhanced" to hear its effect on the open recording.
  }
}

// Play the open recording through the active enhancer (raw if none active):
// the audio the recognizer (STT) would receive. This is the enhancer's only
// effect — it does not change the VAD engines' segments.
function playEnhanced() {
  if (!hasRecordedSession()) return;
  const active = state.enhancers.some((e) => e.enabled);
  audio.src = active
    ? `/api/sessions/${state.currentSession}/enhanced.wav?t=${Date.now()}`
    : `/api/sessions/${state.currentSession}/audio.wav`;
  audio.play().catch(() => {});
}

/* ---------- wiring ---------- */

timeline.onSeek = (t) => {
  if (audio.src && audio.style.display !== "none") audio.currentTime = t / 1000;
};
audio.addEventListener("timeupdate", () => timeline.setPlayhead(audio.currentTime * 1000));
audio.addEventListener("ended", () => timeline.setPlayhead(null));

els.followBtn.onclick = () => {
  timeline.follow = !timeline.follow;
  els.followBtn.classList.toggle("on", timeline.follow);
  timeline._followLive();
};
els.fitBtn.onclick = () => timeline.fit();
els.reanalyzeBtn.onclick = reanalyzeAll;

// calendar day filter for the session list (filters the already-fetched list)
els.sessionDate.onchange = () => {
  state.sessionFilterDate = els.sessionDate.value || null;
  renderSessionList();
};
els.sessionDateClear.onclick = () => {
  els.sessionDate.value = "";
  state.sessionFilterDate = null;
  renderSessionList();
};

refreshEngines();
refreshEnhancers();
refreshSessions().then(async () => {
  const first = els.sessionList.querySelector("li[data-id]");
  if (first) openSession(first.dataset.id);
});
connectWs();
pollSoftphone();
