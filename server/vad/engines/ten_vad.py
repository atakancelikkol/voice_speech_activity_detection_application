"""ten_vad — TEN framework VAD via the `ten-vad` pip package (prebuilt lib).

16 kHz, 256-sample (16 ms) hops, probability output. Segmentation uses the
shared ProbabilityHysteresis; the package's own binary flag is ignored.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from server.vad.base import AudioFormat, FrameScore, ParamSpec, VadEngine
from server.vad.segments import ProbabilityHysteresis

SAMPLE_RATE = 16000
HOP = 256  # 16 ms


class Engine(VadEngine):
    name = "ten_vad"
    display_name = "TEN VAD"
    params = [
        ParamSpec("threshold", "Speech threshold", "float", 0.5, 0.05, 0.95, 0.05),
        ParamSpec("min_speech_ms", "Min speech", "int", 250, 0, 2000, 10, "ms"),
        ParamSpec("min_silence_ms", "Min silence", "int", 300, 0, 5000, 10, "ms"),
        ParamSpec("speech_pad_ms", "Speech padding", "int", 30, 0, 500, 10, "ms"),
    ]

    _FORMAT = AudioFormat(sample_rate=SAMPLE_RATE, frame_samples=HOP)

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        try:
            from ten_vad import TenVad  # noqa: F401
        except Exception as exc:  # ImportError or the prebuilt lib failing to load
            return False, f"ten-vad package unavailable: {exc}"
        return True, ""

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        from ten_vad import TenVad

        self._vad = TenVad(HOP, self.config["threshold"])
        self._hysteresis = ProbabilityHysteresis(
            threshold=self.config["threshold"],
            min_speech_ms=self.config["min_speech_ms"],
            min_silence_ms=self.config["min_silence_ms"],
            speech_pad_ms=self.config["speech_pad_ms"],
        )

    @property
    def input_format(self) -> AudioFormat:
        return self._FORMAT

    def process(self, frame: np.ndarray, frame_start_ms: float) -> FrameScore:
        prob, _flag = self._vad.process(np.ascontiguousarray(frame, dtype=np.int16))
        prob = float(prob)
        event = self._hysteresis.update(prob, frame_start_ms, self._FORMAT.frame_ms)
        return FrameScore(score=prob, raw=prob, event=event)

    def reset(self) -> None:
        from ten_vad import TenVad

        self._vad = TenVad(HOP, self.config["threshold"])
        self._hysteresis.reset()
