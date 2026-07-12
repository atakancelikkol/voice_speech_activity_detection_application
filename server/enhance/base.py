"""Audio enhancer plugin interface — a stage that cleans the 8/16 kHz mono
stream sent to the recognizer (STT), mirroring UniMRCP. It is decoupled from
the VAD: the engines always score the raw audio, and the enhancer only feeds
the /enhanced.wav "what the recognizer would hear" preview.

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


def enhance_pcm(enhancer: "AudioEnhancer", pcm: np.ndarray, frame_samples: int = 160) -> np.ndarray:
    """Run an enhancer over a whole 8 kHz buffer, frame by frame (the enhancer
    is stateful). Used by the /enhanced.wav preview to render the audio the
    recognizer would receive. Returns a same-length enhanced copy."""
    hint = SpeechHint(enhancer.sample_rate)
    out = [enhancer.process(pcm[i : i + frame_samples], hint.update(pcm[i : i + frame_samples]))
           for i in range(0, len(pcm), frame_samples)]
    return np.concatenate(out) if out else pcm.copy()


class SpeechHint:
    """Coarse energy-based speech flag to drive the enhancer's noise learning.

    The enhancer is decoupled from the VAD engines, so it has no detector to
    gate its noise/AGC adaptation and needs its own hint. A leaky noise-floor
    tracker with hysteresis: a frame counts as speech when its RMS sits well
    above the tracked floor.
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
