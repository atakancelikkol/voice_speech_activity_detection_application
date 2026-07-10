"""Turning engine events into speech segments, plus the shared hysteresis
state machine used by probability-based engines (silero, ten)."""

from __future__ import annotations

from dataclasses import dataclass

from server.vad.base import EventKind, VadEvent


@dataclass
class Segment:
    start_ms: float
    end_ms: float
    final: bool = True

    def as_dict(self) -> dict:
        return {"start_ms": round(self.start_ms, 1), "end_ms": round(self.end_ms, 1), "final": self.final}


class SegmentBuilder:
    """Collects SPEECH_START/SPEECH_END events into [start_ms, end_ms] segments.

    Event timestamps may be backdated, so a new segment can begin before the
    frame that produced the event.
    """

    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self._open_start: float | None = None

    def on_event(self, event: VadEvent) -> None:
        if event.kind is EventKind.SPEECH_START and self._open_start is None:
            self._open_start = event.at_ms
        elif event.kind is EventKind.SPEECH_END and self._open_start is not None:
            start = self._open_start
            self._open_start = None
            self.segments.append(Segment(start, max(event.at_ms, start)))

    def open_segment(self, now_ms: float) -> Segment | None:
        """The provisional (not yet closed) segment, if speech is ongoing."""
        if self._open_start is None:
            return None
        return Segment(self._open_start, max(now_ms, self._open_start), final=False)

    def finalize(self, end_ms: float) -> None:
        """Close a still-open segment at end of stream."""
        if self._open_start is not None:
            self.segments.append(Segment(self._open_start, max(end_ms, self._open_start)))
            self._open_start = None


class ProbabilityHysteresis:
    """Onset/offset decision over per-frame speech probabilities.

    Speech starts once the probability stays >= threshold for min_speech_ms
    (SPEECH_START backdated to the start of that run, padded by
    speech_pad_ms); it ends once the probability stays below threshold for
    min_silence_ms (SPEECH_END backdated to where silence began, padded).
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_ms: float = 250.0,
        min_silence_ms: float = 300.0,
        speech_pad_ms: float = 30.0,
    ) -> None:
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.min_silence_ms = min_silence_ms
        self.speech_pad_ms = speech_pad_ms
        self._speaking = False
        self._run_start: float | None = None

    def update(self, prob: float, frame_start_ms: float, frame_ms: float) -> VadEvent | None:
        above = prob >= self.threshold
        frame_end_ms = frame_start_ms + frame_ms
        if not self._speaking:
            if above:
                if self._run_start is None:
                    self._run_start = frame_start_ms
                if frame_end_ms - self._run_start >= self.min_speech_ms:
                    self._speaking = True
                    start = max(0.0, self._run_start - self.speech_pad_ms)
                    self._run_start = None
                    return VadEvent(EventKind.SPEECH_START, start)
            else:
                self._run_start = None
        else:
            if not above:
                if self._run_start is None:
                    self._run_start = frame_start_ms
                if frame_end_ms - self._run_start >= self.min_silence_ms:
                    self._speaking = False
                    end = self._run_start + self.speech_pad_ms
                    self._run_start = None
                    return VadEvent(EventKind.SPEECH_END, end)
            else:
                self._run_start = None
        return None

    def reset(self) -> None:
        self._speaking = False
        self._run_start = None
