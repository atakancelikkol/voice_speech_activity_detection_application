"""Incremental WAV recorder (header is patched on close by the wave module)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


class WavWriter:
    def __init__(self, path: str | Path, rate: int = 8000):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._wf = wave.open(str(path), "wb")
        self._wf.setnchannels(1)
        self._wf.setsampwidth(2)
        self._wf.setframerate(rate)
        self.samples_written = 0

    def append(self, pcm: np.ndarray) -> None:
        self._wf.writeframes(np.asarray(pcm, dtype="<i2").tobytes())
        self.samples_written += len(pcm)

    def close(self) -> None:
        if self._wf is not None:
            self._wf.close()
            self._wf = None
