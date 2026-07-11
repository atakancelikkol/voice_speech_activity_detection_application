import { Timeline } from "./timeline.js";

const ENGINE_COLORS = {
  unimrcp_vad: "#ffa94d",
  silero_vad: "#69db7c",
  ten_vad: "#da77f2",
};
const FALLBACK_COLORS = ["#4dabf7", "#f783ac", "#a9e34b", "#ffd43b"];

const state = {
  engines: [],
  currentSession: null, // session id being viewed
  liveSession: null,
  annotationsDirty: false,
};

const timeline = new Timeline(document.getElementById("timeline"));
window.vadTimeline = timeline; // console/debug access
const audio = document.getElementById("player");
const els = {
  sessionList: document.getElementById("sessionList"),
  liveBadge: document.getElementById("liveBadge"),
  title: document.getElementById("sessionTitle"),
  followBtn: document.getElementById("followBtn"),
  fitBtn: document.getElementById("fitBtn"),
  reanalyzeBtn: document.getElementById("reanalyzeBtn"),
  annotateBtn: document.getElementById("annotateBtn"),
  saveAnnoBtn: document.getElementById("saveAnnoBtn"),
  enginePanel: document.getElementById("enginePanel"),
  metrics: document.getElementById("metrics"),
  recordBtn: document.getElementById("recordBtn"),
  wavBtn: document.getElementById("wavBtn"),
  recHint: document.getElementById("recHint"),
  recLevelFill: document.getElementById("recLevelFill"),
};

function colorOf(name) {
  if (!ENGINE_COLORS[name]) {
    ENGINE_COLORS[name] = FALLBACK_COLORS[Object.keys(ENGINE_COLORS).length % FALLBACK_COLORS.length];
  }
  return ENGINE_COLORS[name];
}

/* ---------- sessions ---------- */

async function refreshSessions() {
  const sessions = await (await fetch("/api/sessions")).json();
  els.sessionList.innerHTML = "";
  for (const s of sessions) {
    const li = document.createElement("li");
    const when = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : s.id;
    li.innerHTML = `${when} ${s.annotated ? '<span class="dot">&#9679;</span>' : ""}
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
  state.annotationsDirty = false;
  const lanes = Object.entries(session.engines).map(([name, r]) => ({
    name,
    color: colorOf(name),
    points: gridToPoints(r.scores),
    segments: r.segments.map((s) => ({ ...s })),
    events: (r.events || []).filter((e) => e.kind === "noinput").map((e) => ({ kind: e.kind, at: e.at_ms })),
  }));
  timeline.setModel({
    duration: session.duration_ms,
    peaks: { dt: session.peaks.dt_ms, values: session.peaks.values },
    lanes,
    annotations: (session.annotations?.speech_regions || []).map((r) => ({ ...r })),
    live: false,
  });
  if (preserveView) {
    timeline.view = view;
    timeline.requestRender();
  }
  els.title.textContent = `${session.id} (${(session.duration_ms / 1000).toFixed(1)}s)`;
  audio.src = `/api/sessions/${session.id}/audio.wav`;
  audio.style.display = "";
  els.reanalyzeBtn.style.display = "";
  setAnnotationEditing(false);
  renderMetrics();
  refreshSessions();
}

function gridToPoints(scores) {
  if (!scores) return [];
  return scores.values.map((v, i) => [scores.t0_ms + i * scores.dt_ms, v]);
}

/* ---------- live updates ---------- */

function startLiveView(sessionId) {
  state.currentSession = sessionId;
  timeline.setModel({ duration: 0, peaks: { dt: 10, values: [] }, lanes: [], annotations: [], live: true });
  timeline.view.msPerPx = 20;
  timeline.follow = true;
  els.title.textContent = `${sessionId} - LIVE`;
  audio.style.display = "none";
  setAnnotationEditing(false);
  els.metrics.innerHTML = "";
  els.reanalyzeBtn.style.display = "none"; // no offline re-run during a live call
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
      }
      break;
    case "audio_peaks":
      if (msg.session_id === state.currentSession) timeline.appendPeaks(msg.t0_ms, msg.dt_ms, msg.peaks);
      break;
    case "scores":
      if (msg.session_id === state.currentSession)
        timeline.appendScores(msg.engine, colorOf(msg.engine), msg.points);
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
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connectWs, 1500);
}

/* ---------- one-button recorder (drives the softphone client) ---------- */

const recorder = { running: false, state: "idle", busy: false };

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
  try {
    const data = await (await fetch("/api/softphone")).json();
    recorder.running = data.running;
    recorder.state = data.status?.state || "idle";
    renderRecorder(data.status);
  } catch {
    recorder.running = false;
    renderRecorder(null);
  }
  setTimeout(pollSoftphone, recorder.state === "idle" ? 2000 : 300);
}

els.recordBtn.onclick = async () => {
  if (recorder.busy) return;
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

els.wavBtn.onclick = async () => {
  if (recorder.busy || recorder.state !== "idle") return;
  const path = prompt("Path to a WAV file on this machine:", "tests/fixtures/speech.wav");
  if (!path) return;
  recorder.busy = true;
  els.wavBtn.disabled = true;
  try {
    setRecHint("streaming the file through the SIP path…", false);
    const res = await fetch("/api/softphone/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "wav", wav_path: path }),
    });
    if (!res.ok) setRecHint(await readErrorDetail(res), true);
    else recorder.state = "active"; // the file plays out, then the call ends on its own
  } catch (err) {
    setRecHint(`request failed: ${err.message || err}`, true);
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
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = engine.enabled;
    toggle.disabled = !engine.available;
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
        inputs[spec.name] = input;
        grid.append(label, input);
      }
      card.appendChild(grid);
      const apply = document.createElement("button");
      apply.className = "apply";
      // with a recorded session open, tuning re-runs it offline immediately;
      // otherwise the params just wait for the next live call
      apply.textContent = hasRecordedSession() ? "Re-analyze recording" : "Apply (next call)";
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

/* ---------- annotations + metrics ---------- */

function setAnnotationEditing(on) {
  timeline.annotationEditing = on;
  els.annotateBtn.classList.toggle("on", on);
  els.saveAnnoBtn.style.display = on || state.annotationsDirty ? "" : "none";
  timeline.requestRender();
}

async function saveAnnotations() {
  if (!state.currentSession) return;
  const regions = timeline.annotations.map((r) => ({
    start_ms: Math.round(r.start_ms * 10) / 10,
    end_ms: Math.round(r.end_ms * 10) / 10,
  }));
  const res = await fetch(`/api/sessions/${state.currentSession}/annotations`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ speech_regions: regions }),
  });
  if (res.ok) {
    state.annotationsDirty = false;
    els.saveAnnoBtn.textContent = "Saved";
    setTimeout(() => (els.saveAnnoBtn.textContent = "Save annotations"), 1200);
    renderMetrics();
    refreshSessions();
  }
}

function renderMetrics() {
  const annotations = timeline.annotations;
  els.metrics.innerHTML = "";
  if (!annotations.length || timeline.live || !timeline.lanes.length) return;
  const grid = 10;
  const n = Math.ceil(timeline.duration / grid);
  const truth = maskOf(annotations, n, grid);
  let html = "<table><tr><th>engine</th><th>prec</th><th>recall</th><th>F1</th></tr>";
  for (const lane of timeline.lanes) {
    const pred = maskOf(
      lane.segments.filter(Boolean).map((s) => ({ start_ms: s.start_ms, end_ms: s.end_ms })),
      n,
      grid
    );
    let tp = 0, fp = 0, fn = 0;
    for (let i = 0; i < n; i++) {
      if (pred[i] && truth[i]) tp++;
      else if (pred[i]) fp++;
      else if (truth[i]) fn++;
    }
    const prec = tp + fp ? tp / (tp + fp) : 0;
    const rec = tp + fn ? tp / (tp + fn) : 0;
    const f1 = prec + rec ? (2 * prec * rec) / (prec + rec) : 0;
    html += `<tr><td style="color:${lane.color}">${lane.name}</td>
      <td>${(prec * 100).toFixed(1)}%</td><td>${(rec * 100).toFixed(1)}%</td><td>${(f1 * 100).toFixed(1)}%</td></tr>`;
  }
  els.metrics.innerHTML = "<h2>vs ground truth (10ms frames)</h2>" + html + "</table>";
}

function maskOf(regions, n, grid) {
  const mask = new Uint8Array(n);
  for (const r of regions) {
    const a = Math.max(0, Math.floor(r.start_ms / grid));
    const b = Math.min(n, Math.ceil(r.end_ms / grid));
    mask.fill(1, a, b);
  }
  return mask;
}

/* ---------- wiring ---------- */

timeline.onSeek = (t) => {
  if (audio.src && audio.style.display !== "none") audio.currentTime = t / 1000;
};
timeline.onAnnotationsChanged = () => {
  state.annotationsDirty = true;
  els.saveAnnoBtn.style.display = "";
  renderMetrics();
};
audio.addEventListener("timeupdate", () => timeline.setPlayhead(audio.currentTime * 1000));
audio.addEventListener("ended", () => timeline.setPlayhead(null));

els.followBtn.onclick = () => {
  timeline.follow = !timeline.follow;
  els.followBtn.classList.toggle("on", timeline.follow);
  timeline._followLive();
};
els.fitBtn.onclick = () => timeline.fit();
els.annotateBtn.onclick = () => setAnnotationEditing(!timeline.annotationEditing);
els.saveAnnoBtn.onclick = saveAnnotations;
els.reanalyzeBtn.onclick = reanalyzeAll;

refreshEngines();
refreshSessions().then(async () => {
  const first = els.sessionList.querySelector("li");
  if (first) openSession(first.dataset.id);
});
connectWs();
pollSoftphone();
