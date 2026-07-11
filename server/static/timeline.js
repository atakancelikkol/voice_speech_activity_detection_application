/* Timeline: canvas component drawing a shared time axis with
 *  - waveform lane (min/max peak pairs)
 *  - one lane per VAD engine: score curve + detected speech segment bars
 *  - ground-truth annotation lane (drag-editable when enabled)
 * Supports live append, wheel zoom, drag pan, click seek, follow-live.
 */

const AXIS_H = 24;
const LABEL_W = 118;
const WAVE_H = 96;
const LANE_H = 74;
const ANNO_H = 46;

const GRID_COLOR = "#242933";
const TEXT_COLOR = "#9aa";
const WAVE_COLOR = "#74c0fc";
const ANNO_COLOR = "#ff6b6b";

export class Timeline {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.reset();
    this.playhead = null;
    this.follow = true;
    this.onSeek = null;
    this.onAnnotationsChanged = null;
    this.annotationEditing = false;
    this._drag = null;
    this._raf = null;
    this._bind();
    new ResizeObserver(() => this.requestRender()).observe(canvas.parentElement);
  }

  reset() {
    this.duration = 0;
    this.peaks = { dt: 10, values: [] };
    this.lanes = []; // {name,color,points:[[t,score]],segments:[],events:[]}
    this.annotations = []; // {start_ms,end_ms}
    this.live = false;
    this.view = { tLeft: 0, msPerPx: 20 };
  }

  laneByName(name) {
    return this.lanes.find((l) => l.name === name);
  }

  ensureLane(name, color) {
    let lane = this.laneByName(name);
    if (!lane) {
      lane = { name, color, points: [], segments: [], events: [] };
      this.lanes.push(lane);
      this._resize();
    }
    return lane;
  }

  setModel({ duration, peaks, lanes, annotations, live }) {
    this.reset();
    this.duration = duration || 0;
    if (peaks) this.peaks = peaks;
    this.lanes = lanes || [];
    this.annotations = annotations || [];
    this.live = !!live;
    this._resize();
    this.fit();
  }

  appendPeaks(t0, dt, pairs) {
    this.peaks.dt = dt;
    const startIdx = Math.round(t0 / dt);
    for (let i = 0; i < pairs.length; i++) this.peaks.values[startIdx + i] = pairs[i];
    this.duration = Math.max(this.duration, (startIdx + pairs.length) * dt);
    this._followLive();
  }

  appendScores(name, color, points) {
    const lane = this.ensureLane(name, color);
    lane.points.push(...points);
    if (points.length) this.duration = Math.max(this.duration, points[points.length - 1][0]);
    this._followLive();
  }

  upsertSegment(name, color, index, seg) {
    const lane = this.ensureLane(name, color);
    lane.segments[index] = seg;
    this.requestRender();
  }

  addEvent(name, color, kind, at) {
    if (kind !== "noinput") return; // start/end are visible as segments
    this.ensureLane(name, color).events.push({ kind, at });
    this.requestRender();
  }

  setPlayhead(ms) {
    this.playhead = ms;
    this.requestRender();
  }

  fit() {
    const w = this._plotWidth();
    this.view.msPerPx = Math.max(0.5, (this.duration || 10000) / Math.max(1, w));
    this.view.tLeft = 0;
    this.requestRender();
  }

  _followLive() {
    if (this.live && this.follow) {
      const w = this._plotWidth();
      this.view.tLeft = Math.max(0, this.duration - w * this.view.msPerPx);
    }
    this.requestRender();
  }

  /* ---------- geometry ---------- */

  _plotWidth() {
    // use the parent's width like _render does: the canvas element itself can
    // report clientWidth 0 depending on layout, which broke Fit (it sized the
    // whole recording into a 50px fallback and zoomed way out)
    return Math.max(50, this.canvas.parentElement.clientWidth - LABEL_W - 8);
  }

  _height() {
    return AXIS_H + WAVE_H + this.lanes.length * LANE_H + ANNO_H + 6;
  }

  _resize() {
    const parent = this.canvas.parentElement;
    parent.style.minHeight = this._height() + "px";
  }

  _xOf(t) {
    return LABEL_W + (t - this.view.tLeft) / this.view.msPerPx;
  }

  _tOf(x) {
    return this.view.tLeft + (x - LABEL_W) * this.view.msPerPx;
  }

  _annoTop() {
    return AXIS_H + WAVE_H + this.lanes.length * LANE_H;
  }

  /* ---------- rendering ---------- */

  requestRender() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = null;
      this._render();
    });
  }

  _render() {
    const dpr = window.devicePixelRatio || 1;
    const cssW = this.canvas.parentElement.clientWidth;
    const cssH = Math.max(this.canvas.parentElement.clientHeight, this._height());
    if (this.canvas.width !== cssW * dpr || this.canvas.height !== cssH * dpr) {
      this.canvas.width = cssW * dpr;
      this.canvas.height = cssH * dpr;
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    this._drawAxis(ctx, cssW);
    this._drawWaveLane(ctx, cssW);
    this.lanes.forEach((lane, i) => this._drawEngineLane(ctx, cssW, lane, AXIS_H + WAVE_H + i * LANE_H));
    this._drawAnnotationLane(ctx, cssW);
    this._drawPlayhead(ctx, cssH);
  }

  _drawLaneFrame(ctx, w, top, h, label, color) {
    ctx.strokeStyle = GRID_COLOR;
    ctx.beginPath();
    ctx.moveTo(0, top + h + 0.5);
    ctx.lineTo(w, top + h + 0.5);
    ctx.stroke();
    ctx.fillStyle = color || TEXT_COLOR;
    ctx.font = "12px system-ui";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, 10, top + h / 2);
  }

  _timeStep() {
    const targetPx = 90;
    const steps = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000];
    for (const s of steps) if (s / this.view.msPerPx >= targetPx) return s;
    return steps[steps.length - 1];
  }

  _drawAxis(ctx, w) {
    ctx.fillStyle = TEXT_COLOR;
    ctx.font = "11px system-ui";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const step = this._timeStep();
    const t0 = Math.floor(this.view.tLeft / step) * step;
    const tEnd = this._tOf(w);
    ctx.strokeStyle = GRID_COLOR;
    for (let t = t0; t <= tEnd; t += step) {
      const x = this._xOf(t);
      if (x < LABEL_W) continue;
      ctx.beginPath();
      ctx.moveTo(x, AXIS_H - 6);
      ctx.lineTo(x, this._annoTop() + ANNO_H);
      ctx.stroke();
      ctx.fillText(fmtTime(t), x, 4);
    }
  }

  _drawWaveLane(ctx, w) {
    const top = AXIS_H;
    this._drawLaneFrame(ctx, w, top, WAVE_H, "waveform", WAVE_COLOR);
    const mid = top + WAVE_H / 2;
    const amp = WAVE_H / 2 - 6;
    ctx.strokeStyle = WAVE_COLOR;
    ctx.beginPath();
    const dt = this.peaks.dt;
    for (let x = LABEL_W; x < w; x++) {
      const tA = this._tOf(x);
      const tB = tA + this.view.msPerPx;
      const iA = Math.max(0, Math.floor(tA / dt));
      const iB = Math.min(this.peaks.values.length, Math.max(iA + 1, Math.ceil(tB / dt)));
      if (iA >= this.peaks.values.length || iB <= 0) continue;
      let lo = 32767, hi = -32768;
      for (let i = iA; i < iB; i++) {
        const p = this.peaks.values[i];
        if (!p) continue;
        if (p[0] < lo) lo = p[0];
        if (p[1] > hi) hi = p[1];
      }
      if (lo > hi) continue;
      ctx.moveTo(x + 0.5, mid - (hi / 32768) * amp);
      ctx.lineTo(x + 0.5, mid - (lo / 32768) * amp + 1);
    }
    ctx.stroke();
  }

  _drawEngineLane(ctx, w, lane, top) {
    this._drawLaneFrame(ctx, w, top, LANE_H, lane.name, lane.color);
    const bottom = top + LANE_H - 6;
    const scoreH = LANE_H - 26;

    // segment bars
    for (const seg of lane.segments) {
      if (!seg) continue;
      const x0 = Math.max(LABEL_W, this._xOf(seg.start_ms));
      const x1 = Math.min(w, this._xOf(seg.end_ms));
      if (x1 < LABEL_W || x0 > w) continue;
      ctx.fillStyle = lane.color + (seg.final ? "46" : "28");
      ctx.fillRect(x0, top + 6, x1 - x0, LANE_H - 12);
      ctx.strokeStyle = lane.color;
      if (!seg.final) ctx.setLineDash([4, 3]);
      ctx.strokeRect(x0 + 0.5, top + 6.5, x1 - x0 - 1, LANE_H - 13);
      ctx.setLineDash([]);
    }

    // score curve
    if (lane.points.length) {
      ctx.strokeStyle = lane.color;
      ctx.beginPath();
      let started = false;
      const tEnd = this._tOf(w);
      for (const [t, s] of lane.points) {
        if (t < this.view.tLeft - 100 || t > tEnd + 100) {
          if (started && t > tEnd + 100) break;
          continue;
        }
        const x = this._xOf(t);
        const y = bottom - s * scoreH;
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    // noinput markers
    for (const ev of lane.events) {
      const x = this._xOf(ev.at);
      if (x < LABEL_W || x > w) continue;
      ctx.strokeStyle = "#ffd43b";
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(x, top + 4);
      ctx.lineTo(x, top + LANE_H - 4);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#ffd43b";
      ctx.font = "10px system-ui";
      ctx.textAlign = "left";
      ctx.fillText("noinput", x + 3, top + 12);
    }
  }

  _drawAnnotationLane(ctx, w) {
    const top = this._annoTop();
    this._drawLaneFrame(ctx, w, top, ANNO_H, "ground truth", ANNO_COLOR);
    for (const region of this.annotations) {
      const x0 = Math.max(LABEL_W, this._xOf(region.start_ms));
      const x1 = Math.min(w, this._xOf(region.end_ms));
      if (x1 < LABEL_W || x0 > w) continue;
      ctx.fillStyle = ANNO_COLOR + "55";
      ctx.fillRect(x0, top + 6, x1 - x0, ANNO_H - 12);
      ctx.strokeStyle = ANNO_COLOR;
      ctx.strokeRect(x0 + 0.5, top + 6.5, x1 - x0 - 1, ANNO_H - 13);
      if (this.annotationEditing) {
        ctx.fillStyle = ANNO_COLOR;
        ctx.fillRect(x0, top + 6, 3, ANNO_H - 12);
        ctx.fillRect(x1 - 3, top + 6, 3, ANNO_H - 12);
      }
    }
    if (this.annotationEditing) {
      ctx.fillStyle = TEXT_COLOR;
      ctx.font = "10px system-ui";
      ctx.textAlign = "left";
      ctx.fillText("drag to add - edges resize - double-click deletes", LABEL_W + 8, top + ANNO_H - 10);
    }
  }

  _drawPlayhead(ctx, h) {
    if (this.playhead == null) return;
    const x = this._xOf(this.playhead);
    if (x < LABEL_W) return;
    ctx.strokeStyle = "#fff";
    ctx.beginPath();
    ctx.moveTo(x, AXIS_H - 8);
    ctx.lineTo(x, h);
    ctx.stroke();
  }

  /* ---------- interaction ---------- */

  _bind() {
    const canvas = this.canvas;
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const t = this._tOf(e.offsetX);
      const factor = Math.exp(e.deltaY * 0.0018);
      const maxScale = Math.max(1, (this.duration || 10000) / this._plotWidth()) * 1.5;
      this.view.msPerPx = Math.min(Math.max(this.view.msPerPx * factor, 0.5), maxScale);
      this.view.tLeft = Math.max(0, t - (e.offsetX - LABEL_W) * this.view.msPerPx);
      this.follow = false;
      this.requestRender();
    }, { passive: false });

    canvas.addEventListener("mousedown", (e) => {
      const t = this._tOf(e.offsetX);
      if (this.annotationEditing && this._inAnnoLane(e.offsetY)) {
        this._drag = this._annoDragFor(t);
      } else {
        this._drag = { kind: "pan", startX: e.offsetX, startTLeft: this.view.tLeft, moved: false };
      }
    });

    window.addEventListener("mousemove", (e) => {
      if (!this._drag) return;
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const t = Math.max(0, Math.min(this.duration, this._tOf(x)));
      const drag = this._drag;
      if (drag.kind === "pan") {
        const dx = x - drag.startX;
        if (Math.abs(dx) > 3) drag.moved = true;
        this.view.tLeft = Math.max(0, drag.startTLeft - dx * this.view.msPerPx);
        if (drag.moved) this.follow = false;
      } else if (drag.kind === "create") {
        drag.region.start_ms = Math.min(drag.anchor, t);
        drag.region.end_ms = Math.max(drag.anchor, t);
        drag.moved = true;
      } else if (drag.kind === "resize-start") {
        drag.region.start_ms = Math.min(t, drag.region.end_ms - 20);
        drag.moved = true;
      } else if (drag.kind === "resize-end") {
        drag.region.end_ms = Math.max(t, drag.region.start_ms + 20);
        drag.moved = true;
      } else if (drag.kind === "move") {
        const span = drag.region.end_ms - drag.region.start_ms;
        let start = t - drag.grabOffset;
        start = Math.max(0, Math.min(start, this.duration - span));
        drag.region.start_ms = start;
        drag.region.end_ms = start + span;
        drag.moved = true;
      }
      this.requestRender();
    });

    window.addEventListener("mouseup", (e) => {
      const drag = this._drag;
      this._drag = null;
      if (!drag) return;
      if (drag.kind === "pan" && !drag.moved) {
        const rect = canvas.getBoundingClientRect();
        const t = this._tOf(e.clientX - rect.left);
        if (this.onSeek && !this.live && t >= 0 && t <= this.duration) this.onSeek(t);
        return;
      }
      if (drag.kind !== "pan" && drag.moved) {
        if (drag.kind === "create" && drag.region.end_ms - drag.region.start_ms < 20) {
          this.annotations = this.annotations.filter((r) => r !== drag.region);
        }
        this.annotations.sort((a, b) => a.start_ms - b.start_ms);
        if (this.onAnnotationsChanged) this.onAnnotationsChanged(this.annotations);
        this.requestRender();
      }
    });

    canvas.addEventListener("dblclick", (e) => {
      if (!this.annotationEditing || !this._inAnnoLane(e.offsetY)) return;
      const t = this._tOf(e.offsetX);
      const hit = this.annotations.find((r) => t >= r.start_ms && t <= r.end_ms);
      if (hit) {
        this.annotations = this.annotations.filter((r) => r !== hit);
        if (this.onAnnotationsChanged) this.onAnnotationsChanged(this.annotations);
        this.requestRender();
      }
    });
  }

  _inAnnoLane(y) {
    const top = this._annoTop();
    return y >= top && y <= top + ANNO_H;
  }

  _annoDragFor(t) {
    const grabMs = 6 * this.view.msPerPx;
    for (const region of this.annotations) {
      if (Math.abs(t - region.start_ms) < grabMs) return { kind: "resize-start", region, moved: false };
      if (Math.abs(t - region.end_ms) < grabMs) return { kind: "resize-end", region, moved: false };
      if (t > region.start_ms && t < region.end_ms)
        return { kind: "move", region, grabOffset: t - region.start_ms, moved: false };
    }
    const region = { start_ms: t, end_ms: t };
    this.annotations.push(region);
    return { kind: "create", region, anchor: t, moved: false };
  }
}

export function fmtTime(ms) {
  const s = ms / 1000;
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${m}:${rest.toFixed(1).padStart(4, "0")}`;
}
