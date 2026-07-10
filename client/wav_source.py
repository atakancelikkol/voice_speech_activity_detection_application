"""Stream a WAV file as real-time 20 ms frames (absolute-deadline pacing)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import numpy as np

from server.audio.wav_io import load_wav

FRAME_SAMPLES = 160  # 20 ms @ 8 kHz
FRAME_INTERVAL = 0.02


async def stream_wav(
    path: str | Path,
    send_frame: Callable[[np.ndarray], None],
    on_level: Callable[[float], None] | None = None,
) -> None:
    pcm = load_wav(path, 8000)
    pad = (-len(pcm)) % FRAME_SAMPLES
    if pad:
        pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
    loop = asyncio.get_running_loop()
    next_deadline = loop.time()
    for start in range(0, len(pcm), FRAME_SAMPLES):
        frame = pcm[start : start + FRAME_SAMPLES]
        send_frame(frame)
        if on_level:
            on_level(float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) / 32768.0))
        next_deadline += FRAME_INTERVAL
        await asyncio.sleep(max(0.0, next_deadline - loop.time()))
