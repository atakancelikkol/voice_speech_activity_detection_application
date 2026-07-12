"""WAV reading/writing helpers (16-bit PCM, mono)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import soxr


def load_wav(path: str | Path, target_rate: int = 8000) -> np.ndarray:
    """Load a WAV file as mono int16 at target_rate."""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.int16)
    elif width == 1:  # 8-bit WAV is unsigned
        samples = ((np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8).astype(np.int16)
    elif width == 4:
        samples = (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
    else:
        raise ValueError(f"unsupported sample width: {width} bytes")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != target_rate:
        samples = soxr.resample(samples, rate, target_rate).astype(np.int16)
    return samples


def save_wav(path: str | Path, samples: np.ndarray, rate: int = 8000) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def wav_bytes(samples: np.ndarray, rate: int = 8000) -> bytes:
    """Serialize mono int16 PCM to an in-memory WAV (for streaming responses)."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()
