"""Microphone capture: device rate -> 8 kHz -> 160-sample (20 ms) frames.

Callbacks fire on the PortAudio thread; the caller must make them
thread-safe (e.g. loop.call_soon_threadsafe). The mic callback is the
pacing clock in mic mode.
"""

from __future__ import annotations

import contextlib
import time
from typing import Callable

import numpy as np
import soxr


class MicCapture:
    def __init__(
        self,
        on_frame: Callable[[np.ndarray], None],
        on_level: Callable[[float], None] | None = None,
        device: int | str | None = None,
    ):
        self.on_frame = on_frame
        self.on_level = on_level
        self.device = device
        self._stream = None
        self._resampler = None
        self._buf = np.empty(0, dtype=np.int16)

    def start(self) -> None:
        import sounddevice as sd  # imported lazily: optional dependency

        stream = self._open_stream(sd)
        rate = int(stream.samplerate)
        self._resampler = None if rate == 8000 else soxr.ResampleStream(rate, 8000, 1, dtype="int16")
        try:
            stream.start()
        except sd.PortAudioError:  # never leave a half-open stream holding the device
            with contextlib.suppress(Exception):
                stream.close()
            raise
        self._stream = stream

    def _open_stream(self, sd):
        """Open the input device, tolerating the transient CoreAudio
        paInternalError (PaErrorCode -9986) macOS raises the first time — often
        while the mic-permission prompt is still resolving, or when a prior
        half-open stream is still being released. Retry a few times (closing
        each failed attempt so it can never orphan the device and lock out the
        next try), then fail with actionable guidance instead of the raw code."""
        last: Exception | None = None
        for _ in range(3):
            try:
                return sd.InputStream(
                    device=self.device,
                    channels=1,
                    dtype="int16",
                    callback=self._callback,
                )
            except sd.PortAudioError as exc:
                last = exc
                time.sleep(0.4)
        raise RuntimeError(
            f"could not open the microphone ({last}). On macOS this usually means the "
            "terminal has no microphone access — grant it in System Settings > Privacy "
            "& Security > Microphone, then fully quit and reopen the terminal. Also make "
            "sure the mic is not held by another app (Zoom/Meet/Photo Booth)."
        ) from last

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _callback(self, indata, frames, time_info, status) -> None:
        mono = indata[:, 0]
        if self.on_level:
            self.on_level(float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)) / 32768.0))
        chunk = mono if self._resampler is None else self._resampler.resample_chunk(mono)
        if not len(chunk):
            return
        self._buf = np.concatenate([self._buf, chunk.astype(np.int16, copy=False)])
        while len(self._buf) >= 160:
            frame, self._buf = self._buf[:160], self._buf[160:]
            self.on_frame(frame)
