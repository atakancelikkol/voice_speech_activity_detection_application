"""Audio enhancer plugin interface — a pre-processing stage that cleans the
8/16 kHz mono stream before it reaches the VAD engines and the recording.

Same shape as the VAD plugin interface (ParamSpec-driven params, probe,
registry) so the UI can render an enhancer the same way it renders an engine.
An enhancer processes 16-bit PCM in place given a per-frame speech hint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np

from server.vad.base import ParamSpec, resolve_params


class AudioEnhancer(ABC):
    name: ClassVar[str]
    display_name: ClassVar[str]
    params: ClassVar[list[ParamSpec]] = []

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        return True, ""

    def __init__(self, sample_rate: int, params: dict[str, Any] | None = None):
        self.sample_rate = sample_rate
        self.config = resolve_params(self.params, params or {})

    @abstractmethod
    def process(self, frame: np.ndarray, is_speech: bool) -> np.ndarray:
        """Return an enhanced copy of one int16 frame. is_speech is a coarse
        VAD hint (noise is learned on non-speech frames)."""

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


class SpeechHint:
    """Coarse energy-based speech flag to drive the enhancer's noise learning.

    The enhancer runs before the real VAD engines, so it needs its own hint.
    A leaky noise-floor tracker with hysteresis: a frame counts as speech when
    its RMS sits well above the tracked floor.
    """

    def __init__(self, sample_rate: int, margin_db: float = 8.0):
        self.margin = 10 ** (margin_db / 20.0)
        self._floor = 50.0
        self._speaking = False

    def update(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) + 1e-9
        # track the floor downward fast, upward slowly, frozen while speaking
        if not self._speaking:
            self._floor = 0.9 * self._floor + 0.1 * rms if rms > self._floor else 0.6 * self._floor + 0.4 * rms
        on = self.margin if not self._speaking else self.margin * 0.6  # hysteresis
        self._speaking = rms > self._floor * on
        return self._speaking

    def reset(self) -> None:
        self._floor = 50.0
        self._speaking = False
