"""df_enhance (DeepFilterNet-structured C enhancer): lib loads, suppresses a
noise-only region while preserving a tone, fixed 2-hop latency, and identical
output regardless of chunking. Probe-guarded so a missing libdfenhance skips
instead of failing (run `make build-c`)."""

from __future__ import annotations

import numpy as np
import pytest

from server.enhance import registry

RATE = 8000
FRAME = 80  # 10 ms, matching the production media frame


def make_enhancer(params=None):
    info = registry.discover().get("df_enhance")
    if info is None or not info.available:
        pytest.skip(info.reason if info else "df_enhance not registered")
    return registry.create(info, RATE, params or {})


def run_frames(enh, pcm, frame=FRAME, hint=False):
    out = [enh.process(pcm[i : i + frame], hint) for i in range(0, len(pcm), frame)]
    return np.concatenate(out)


def make_signal(seconds=4.0, tone_from=2.0, seed=1234):
    """White noise floor throughout; speech-like harmonic BURSTS (200 Hz
    fundamental + harmonics to 2 kHz, 300 ms on / 200 ms off, syllable-ish
    cadence) from tone_from onward. Two deliberate choices: bursts, because a
    steady tone would rightly be learned as stationary noise by the minimum-
    statistics tracker; and a broadband harmonic complex, because a single
    sine falls into one ERB band and the band-gain interpolation smears the
    neighbours' floor into it (~-2 dB) — an inherent ERB-resolution
    characteristic (DFN has it too), not a speech-damage signal.
    Returns (pcm, tone_mask)."""
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    sig = rng.normal(0.0, 300.0, n)
    t = np.arange(n) / RATE
    burst = ((t - tone_from) % 0.5) < 0.3
    mask = (t >= tone_from) & burst
    harm = sum(np.sin(2 * np.pi * f * t + 0.7 * i)
               for i, f in enumerate(range(200, 2001, 200)))
    tone = np.where(mask, 2500.0 * harm, 0.0)
    return np.clip(sig + tone, -32768, 32767).astype(np.int16), mask


def test_lib_probe_and_latency():
    enh = make_enhancer()
    assert enh.latency_samples == 160  # 2 hops = 20 ms at 8 kHz
    enh.close()


def test_noise_suppressed_tone_preserved():
    pcm, mask = make_signal()
    enh = make_enhancer()
    # feed with the per-frame speech hint a real VAD would provide
    out = np.concatenate([
        enh.process(pcm[i : i + FRAME], bool(mask[i : i + FRAME].any()))
        for i in range(0, len(pcm), FRAME)
    ])
    enh.close()
    lat = 160

    # noise-only region (skip the first second of convergence)
    noise_in = pcm[RATE : 2 * RATE].astype(np.float64)
    noise_out = out[RATE + lat : 2 * RATE + lat].astype(np.float64)
    red_db = 20 * np.log10(
        (np.sqrt(np.mean(noise_out**2)) + 1e-9) / (np.sqrt(np.mean(noise_in**2)) + 1e-9)
    )
    assert red_db < -8.0, f"noise reduced only {red_db:.1f} dB"

    # burst region: compare RMS over the tone-on samples (middle of each
    # burst; skip 40 ms edges for onset/offset ramps), delay-aligned
    core = mask.copy()
    edge = int(0.04 * RATE)
    on = np.flatnonzero(np.diff(mask.astype(int)) == 1)
    off = np.flatnonzero(np.diff(mask.astype(int)) == -1)
    for s in on:
        core[s : s + edge] = False
    for e in off:
        core[max(0, e - edge) : e + 1] = False
    idx = np.flatnonzero(core[: len(out) - lat])
    tone_in = pcm[idx].astype(np.float64)
    tone_out = out[idx + lat].astype(np.float64)
    delta_db = 20 * np.log10(
        (np.sqrt(np.mean(tone_out**2)) + 1e-9) / (np.sqrt(np.mean(tone_in**2)) + 1e-9)
    )
    assert abs(delta_db) < 2.0, f"tone RMS changed {delta_db:.1f} dB"


def test_chunk_invariance():
    pcm, _mask = make_signal(seconds=2.0)
    enh = make_enhancer()
    ref = run_frames(enh, pcm)
    enh.reset()
    rng = np.random.default_rng(7)
    out, i = [], 0
    while i < len(pcm):
        n = int(rng.integers(1, 400))
        out.append(enh.process(pcm[i : i + n], False))
        i += n
    enh.close()
    assert np.array_equal(ref, np.concatenate(out))


def test_ten_vad_df_engine_runs():
    """The ten_vad_df lane produces sane scores over synthetic audio."""
    from server.vad import registry as vad_registry

    infos = vad_registry.discover()
    info = infos.get("ten_vad_df")
    if info is None or not info.available:
        pytest.skip(info.reason if info else "ten_vad_df not registered")
    engine = vad_registry.create(info, None)
    pcm, _mask = make_signal(seconds=3.0, tone_from=1.5)
    fmt = engine.input_format
    assert fmt.sample_rate == 8000 and fmt.frame_samples == 80
    scores = []
    for i in range(0, len(pcm) - fmt.frame_samples, fmt.frame_samples):
        fs = engine.process(pcm[i : i + fmt.frame_samples], i / 8.0)
        scores.append(fs.score)
    assert all(0.0 <= s <= 1.0 for s in scores)
    engine.close()
