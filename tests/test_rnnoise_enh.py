"""RNNoise enhancer (xiph, via ctypes): lib loads, wet=0 is a pure delay
(dry passthrough), wet>0 changes the audio, and the ten_vad_rnn lane runs.
Probe-guarded so a missing librnenhance skips (run `make build-c`)."""

from __future__ import annotations

import numpy as np
import pytest

from server.enhance import registry

RATE = 8000
FRAME = 80


def make_enhancer(params=None):
    info = registry.discover().get("rnnoise")
    if info is None or not info.available:
        pytest.skip(info.reason if info else "rnnoise not registered")
    return registry.create(info, RATE, params or {})


def run_frames(enh, pcm):
    return np.concatenate([enh.process(pcm[i : i + FRAME], False)
                           for i in range(0, len(pcm), FRAME)])


def noisy_signal(seconds=3.0, seed=3):
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    sig = rng.normal(0.0, 400.0, n)
    t = np.arange(n) / RATE
    voiced = (t > 1.0) & (t < 2.0)
    harm = sum(np.sin(2 * np.pi * f * t) for f in (220, 440, 660, 880))
    return np.clip(sig + np.where(voiced, 4000.0 * harm, 0.0), -32768, 32767).astype(np.int16)


def test_lib_probe_and_latency():
    enh = make_enhancer()
    assert enh.latency_samples == 288  # 36 ms at 8 kHz (ring + 2 frames + FIR)
    enh.close()


def test_wet_zero_is_delayed_dry():
    """wet=0 must return the input delayed by the reported latency, within a
    small FIR-rounding tolerance."""
    pcm = noisy_signal()
    enh = make_enhancer({"wet": 0.0})
    lat = enh.latency_samples
    out = run_frames(enh, pcm)
    enh.close()
    a = pcm[: len(pcm) - lat].astype(np.float64)
    b = out[lat:].astype(np.float64)
    err = np.sqrt(np.mean((a - b) ** 2)) / (np.sqrt(np.mean(a**2)) + 1e-9)
    assert err < 0.02, f"dry passthrough error {err:.4f}"


def test_wet_changes_audio_and_denoises():
    pcm = noisy_signal()
    enh = make_enhancer({"wet": 1.0})
    lat = enh.latency_samples
    out = run_frames(enh, pcm)
    enh.close()
    # noise-only region 2.2-2.9s reduced
    a, b = int(2.2 * RATE), int(2.9 * RATE)
    nin = pcm[a:b].astype(np.float64)
    nout = out[a + lat : b + lat].astype(np.float64)
    red = 20 * np.log10((np.sqrt(np.mean(nout**2)) + 1e-9) / (np.sqrt(np.mean(nin**2)) + 1e-9))
    assert red < -2.0, f"noise reduced only {red:.1f} dB"


def test_chunk_invariance():
    pcm = noisy_signal(seconds=1.5)
    enh = make_enhancer()
    ref = run_frames(enh, pcm)
    enh.reset()
    rng = np.random.default_rng(11)
    out, i = [], 0
    while i < len(pcm):
        n = int(rng.integers(1, 400))
        out.append(enh.process(pcm[i : i + n], False))
        i += n
    enh.close()
    assert np.array_equal(ref, np.concatenate(out))


def test_ten_vad_rnn_engine_runs():
    from server.vad import registry as vad_registry

    info = vad_registry.discover().get("ten_vad_rnn")
    if info is None or not info.available:
        pytest.skip(info.reason if info else "ten_vad_rnn not registered")
    engine = vad_registry.create(info, None)
    pcm = noisy_signal()
    fmt = engine.input_format
    assert fmt.sample_rate == 8000 and fmt.frame_samples == 80
    scores = [engine.process(pcm[i : i + fmt.frame_samples], i / 8.0).score
              for i in range(0, len(pcm) - fmt.frame_samples, fmt.frame_samples)]
    assert all(0.0 <= s <= 1.0 for s in scores)
    engine.close()
