"""EngineRunner: adapts the 8 kHz session stream to each engine's format and
owns the mapping of engine frames back onto the session timeline."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import soxr

from server.vad.base import EventKind, VadEngine, VadEvent
from server.vad.segments import Segment, SegmentBuilder

SOURCE_RATE = 8000


@dataclass
class TimedScore:
    t_ms: float
    frame_ms: float
    score: float
    raw: float
    event: VadEvent | None


class EngineRunner:
    """Feeds sequential 8 kHz int16 audio to one engine.

    The session stream must be gapless (RTP losses are filled with silence
    upstream), so time is derived purely from sample counts: source position
    for bookkeeping, engine-rate position for frame timestamps.
    """

    def __init__(self, engine: VadEngine, source_rate: int = SOURCE_RATE) -> None:
        self.engine = engine
        self.fmt = engine.input_format
        self.builder = SegmentBuilder()
        self.events: list[VadEvent] = []
        self.source_rate = source_rate
        self._resampler = (
            None
            if self.fmt.sample_rate == source_rate
            else soxr.ResampleStream(source_rate, self.fmt.sample_rate, 1, dtype="int16")
        )
        self._buf = np.empty(0, dtype=np.int16)
        self._out_pos = 0  # engine-rate samples consumed
        self._src_samples = 0

    @property
    def position_ms(self) -> float:
        return self._src_samples * 1000.0 / self.source_rate

    def feed(self, pcm: np.ndarray) -> list[TimedScore]:
        self._src_samples += len(pcm)
        return self._ingest(pcm, last=False)

    def finalize(self) -> list[Segment]:
        """Flush pending audio, close any open segment, return all segments."""
        if self._resampler is not None:
            self._ingest(np.empty(0, dtype=np.int16), last=True)
        self.builder.finalize(self.position_ms)
        self.engine.close()
        return self.builder.segments

    def _ingest(self, pcm: np.ndarray, last: bool) -> list[TimedScore]:
        if self._resampler is not None:
            pcm = self._resampler.resample_chunk(pcm.astype(np.int16, copy=False), last=last)
        if len(pcm):
            self._buf = np.concatenate([self._buf, pcm.astype(np.int16, copy=False)])
        out: list[TimedScore] = []
        n = self.fmt.frame_samples
        while len(self._buf) >= n:
            frame, self._buf = self._buf[:n], self._buf[n:]
            t_ms = self._out_pos * 1000.0 / self.fmt.sample_rate
            result = self.engine.process(frame, t_ms)
            self._out_pos += n
            if result.event is not None:
                self.events.append(result.event)
                if result.event.kind is not EventKind.NOINPUT:
                    self.builder.on_event(result.event)
            out.append(TimedScore(t_ms, self.fmt.frame_ms, result.score, result.raw, result.event))
        return out
