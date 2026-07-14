"""Raw-PCM ingestion: feed the headless LPCM/8000/1 UniMRCP decodes G.711 into
straight to the pipeline, no WAV container."""

from __future__ import annotations

import numpy as np

from server.audio.wav_io import load_raw_pcm


def test_raw_pcm_round_trips_samples():
    x = np.array([0, 1, -1, 32767, -32768, 1234, -4321], dtype="<i2")
    out = load_raw_pcm(x.tobytes())
    assert out.dtype == np.int16
    assert np.array_equal(out, x)


def test_raw_pcm_drops_odd_trailing_byte():
    x = np.array([5, 6, 7], dtype="<i2")
    out = load_raw_pcm(x.tobytes() + b"\x99")  # 7 bytes: last one is a torn sample
    assert np.array_equal(out, x)


def test_raw_pcm_resamples_to_target():
    x = np.zeros(1600, dtype="<i2")  # 100 ms @ 16 kHz
    out = load_raw_pcm(x.tobytes(), source_rate=16000, target_rate=8000)
    assert abs(len(out) - 800) <= 2  # ~100 ms @ 8 kHz


def test_raw_pcm_empty():
    assert len(load_raw_pcm(b"")) == 0
