"""Per-call audio pipeline and call lifecycle management.

CallPipeline.on_audio runs inside the asyncio loop (datagram callback):
record WAV, bin waveform peaks, feed every enabled engine, batch live
updates to the WebSocket hub roughly every 100 ms.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import numpy as np

from server.analysis import grid_scores
from server.api.ws import Hub
from server.audio.peaks import PeaksBinner
from server.audio.wav_writer import WavWriter
from server.engines_state import EngineManager
from server.rtp.receiver import open_rtp_receiver
from server.sessions.store import SessionStore
from server.vad.base import VadEngine
from server.vad.runner import SOURCE_RATE, EngineRunner, TimedScore

PEAK_BIN_MS = 10
FLUSH_INTERVAL_S = 0.1


class CallPipeline:
    def __init__(
        self,
        session_id: str,
        session_dir: Path,
        engines: dict[str, VadEngine],
        engine_configs: dict[str, dict],
        hub: Hub | None,
        call_id: str,
    ):
        self.session_id = session_id
        self.session_dir = session_dir
        self.call_id = call_id
        self.engine_configs = engine_configs
        self.hub = hub
        self.started_at = time.time()
        self.runners = {name: EngineRunner(engine) for name, engine in engines.items()}
        self.wav = WavWriter(session_dir / "audio.wav", SOURCE_RATE)
        self.binner = PeaksBinner(SOURCE_RATE * PEAK_BIN_MS // 1000)
        self.peaks: list[tuple[int, int]] = []
        self.scores: dict[str, list[TimedScore]] = {name: [] for name in self.runners}
        self.rtp_stats: dict | None = None
        self._pending_peaks: list[tuple[int, int]] = []
        self._pending_scores: dict[str, list[TimedScore]] = {name: [] for name in self.runners}
        self._sent_segments: dict[str, int] = {name: 0 for name in self.runners}
        self._flush_task: asyncio.Task | None = None
        self._finalized = False

    @property
    def duration_ms(self) -> float:
        return self.wav.samples_written * 1000.0 / SOURCE_RATE

    def on_audio(self, pcm: np.ndarray) -> None:
        if self._finalized:
            return
        self.wav.append(pcm)
        new_peaks = self.binner.feed(pcm)
        self.peaks.extend(new_peaks)
        self._pending_peaks.extend(new_peaks)
        for name, runner in self.runners.items():
            scored = runner.feed(pcm)
            self.scores[name].extend(scored)
            self._pending_scores[name].extend(scored)
        if self.hub and self._flush_task is None:
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        self._flush_task = None
        self._flush_live()

    def _flush_live(self) -> None:
        if not self.hub:
            return
        if self._pending_peaks:
            t0 = (len(self.peaks) - len(self._pending_peaks)) * PEAK_BIN_MS
            self.hub.publish(
                {
                    "kind": "audio_peaks",
                    "session_id": self.session_id,
                    "t0_ms": t0,
                    "dt_ms": PEAK_BIN_MS,
                    "peaks": self._pending_peaks,
                }
            )
            self._pending_peaks = []
        for name, runner in self.runners.items():
            pending = self._pending_scores[name]
            if pending:
                self.hub.publish(
                    {
                        "kind": "scores",
                        "session_id": self.session_id,
                        "engine": name,
                        "points": [[round(s.t_ms, 1), round(s.score, 4)] for s in pending],
                    }
                )
                self._pending_scores[name] = []
                for s in pending:
                    if s.event:
                        self.hub.publish(
                            {
                                "kind": "event",
                                "session_id": self.session_id,
                                "engine": name,
                                "event": s.event.kind.value,
                                "at_ms": round(s.event.at_ms, 1),
                            }
                        )
            # closed segments not yet announced
            segments = runner.builder.segments
            while self._sent_segments[name] < len(segments):
                index = self._sent_segments[name]
                seg = segments[index]
                self._publish_segment(name, index, seg.start_ms, seg.end_ms, final=True)
                self._sent_segments[name] += 1
            open_seg = runner.builder.open_segment(runner.position_ms)
            if open_seg is not None:
                self._publish_segment(
                    name, self._sent_segments[name], open_seg.start_ms, open_seg.end_ms, final=False
                )

    def _publish_segment(self, engine: str, index: int, start_ms: float, end_ms: float, final: bool) -> None:
        self.hub.publish(
            {
                "kind": "segment",
                "session_id": self.session_id,
                "engine": engine,
                "index": index,
                "start_ms": round(start_ms, 1),
                "end_ms": round(end_ms, 1),
                "final": final,
            }
        )

    def finalize(self) -> dict:
        if self._finalized:
            raise RuntimeError("pipeline already finalized")
        self._finalized = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        engines_payload = {}
        for name, runner in self.runners.items():
            segments = runner.finalize()
            engines_payload[name] = {
                "config": self.engine_configs.get(name, {}),
                "segments": [seg.as_dict() for seg in segments],
                "events": [{"kind": e.kind.value, "at_ms": round(e.at_ms, 1)} for e in runner.events],
                "scores": self._grid_scores(self.scores[name]),
            }
        duration = self.duration_ms
        self.wav.close()
        payload = {
            "id": self.session_id,
            "call_id": self.call_id,
            "started_at": self.started_at,
            "duration_ms": round(duration, 1),
            "sample_rate": SOURCE_RATE,
            "peaks": {"t0_ms": 0, "dt_ms": PEAK_BIN_MS, "values": self.peaks},
            "rtp": self.rtp_stats or {},
            "engines": engines_payload,
        }
        if self.hub:
            self._flush_live()
        return payload

    def _grid_scores(self, scores: list[TimedScore]) -> dict:
        return grid_scores(scores, self.duration_ms)


RTP_IDLE_TIMEOUT_S = 30.0
WATCHDOG_INTERVAL_S = 5.0


class CallManager:
    """Owns RTP ports and the pipeline for the (single) active call."""

    def __init__(self, config, store: SessionStore, engine_manager: EngineManager, hub: Hub | None):
        self.config = config
        self.store = store
        self.engine_manager = engine_manager
        self.hub = hub
        self.on_call_ended = None  # set by the SIP UAS to drop its dialog state
        self._free_ports = list(range(config.rtp_port_min, config.rtp_port_max + 1, 2))
        self._active: dict[str, dict] = {}  # call_id -> {pipeline, transport, protocol, rtp_port}

    async def start_call(self, call_id: str) -> tuple[int, CallPipeline]:
        if call_id in self._active:
            entry = self._active[call_id]
            return entry["rtp_port"], entry["pipeline"]
        if not self._free_ports:
            raise RuntimeError("no free RTP ports")
        rtp_port = self._free_ports.pop(0)
        session_id, session_dir = self.store.new_session_dir(call_id)
        engines = self.engine_manager.instantiate_enabled()
        pipeline = CallPipeline(
            session_id, session_dir, engines, self.engine_manager.active_configs(), self.hub, call_id
        )
        transport, protocol = await open_rtp_receiver(self.config.host, rtp_port, pipeline.on_audio)
        loop = asyncio.get_running_loop()
        self._active[call_id] = {
            "pipeline": pipeline,
            "transport": transport,
            "protocol": protocol,
            "rtp_port": rtp_port,
            "started_at": loop.time(),
            "watchdog": loop.create_task(self._watchdog(call_id)),
        }
        if self.hub:
            self.hub.publish({"kind": "call_state", "state": "active", "session_id": session_id})
        return rtp_port, pipeline

    async def _watchdog(self, call_id: str) -> None:
        """Tear the call down if the peer vanishes without a BYE."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            entry = self._active.get(call_id)
            if entry is None:
                return
            last = entry["protocol"].last_packet_at or entry["started_at"]
            if loop.time() - last > RTP_IDLE_TIMEOUT_S:
                logging.getLogger("calls").warning(
                    "call %s: no RTP for %.0fs, ending", call_id, RTP_IDLE_TIMEOUT_S
                )
                self.end_call(call_id)
                return

    def end_call(self, call_id: str) -> str | None:
        entry = self._active.pop(call_id, None)
        if entry is None:
            return None
        entry["watchdog"].cancel()
        if self.on_call_ended:
            self.on_call_ended(call_id)
        entry["transport"].close()
        pipeline: CallPipeline = entry["pipeline"]
        stats = entry["protocol"].buffer.stats
        pipeline.rtp_stats = vars(stats).copy()
        payload = pipeline.finalize()
        self.store.write_session(pipeline.session_id, payload)
        self._free_ports.append(entry["rtp_port"])
        if self.hub:
            self.hub.publish(
                {"kind": "call_state", "state": "finished", "session_id": pipeline.session_id}
            )
        return pipeline.session_id

    def end_all(self) -> None:
        for call_id in list(self._active):
            self.end_call(call_id)
