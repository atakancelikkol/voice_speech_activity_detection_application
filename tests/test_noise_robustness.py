"""Noise-robustness: run every engine on clean vs noisy versions of the same
speech and measure frame-level precision/recall/F1 against ground truth.

This is the comparison the whole app exists to make visible: a fixed-threshold
energy detector floods with false positives as noise rises, while the neural
engines keep rejecting it. Fixtures come from scripts/make_noisy_wavs.py
(run `make wavs`); real MS-SNSD babble if downloaded, synthetic otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from server.audio.wav_io import load_wav
from server.vad import registry
from server.vad.runner import SOURCE_RATE, EngineRunner

FIXTURES = Path(__file__).parent / "fixtures"
GRID_MS = 10


def _mask(regions, n_frames):
    m = np.zeros(n_frames, dtype=bool)
    for r in regions:
        m[int(r["start_ms"] / GRID_MS) : int(r["end_ms"] / GRID_MS)] = True
    return m


def frame_prf(segments, regions, duration_ms):
    n = int(duration_ms / GRID_MS) + 1
    truth = _mask(regions, n)
    pred = _mask([{"start_ms": s.start_ms, "end_ms": s.end_ms} for s in segments], n)
    tp = int((pred & truth).sum())
    fp = int((pred & ~truth).sum())
    fn = int((~pred & truth).sum())
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def run_engine(name, wav):
    info = registry.discover()[name]
    if not info.available:
        pytest.skip(f"{name} unavailable: {info.reason}")
    runner = EngineRunner(registry.create(info))
    pcm = load_wav(wav, SOURCE_RATE)
    chunk = SOURCE_RATE * 20 // 1000
    for start in range(0, len(pcm), chunk):
        runner.feed(pcm[start : start + chunk])
    segments = runner.finalize()
    duration_ms = len(pcm) * 1000.0 / SOURCE_RATE
    return frame_prf(segments, json.loads(Path(wav).with_suffix(".json").read_text())["speech_regions"], duration_ms)


def require(name):
    wav = FIXTURES / name
    if not wav.exists():
        pytest.skip(f"{name} missing — run `make wavs`")
    return wav


class TestCleanBaseline:
    @pytest.mark.parametrize("engine", ["unimrcp_vad", "silero_vad", "ten_vad", "arf_vad"])
    def test_all_engines_usable_when_clean(self, engine):
        # F1 rather than recall: arf/ten are conservative (high precision,
        # lower recall) even on clean speech, but still clearly usable
        _, _, f1 = run_engine(engine, require("speech.wav"))
        assert f1 > 0.55, f"{engine} barely usable on clean speech (F1={f1:.2f})"


class TestNoiseRobustness:
    def test_energy_detector_loses_precision_under_heavy_noise(self):
        clean_p, _, _ = run_engine("unimrcp_vad", require("speech.wav"))
        noisy_p, _, _ = run_engine("unimrcp_vad", require("noisy_snr5.wav"))
        # a fixed energy threshold marks the noise floor as speech
        assert noisy_p < clean_p - 0.1, f"expected precision drop: clean={clean_p:.2f} noisy={noisy_p:.2f}"

    def test_neural_beats_energy_under_heavy_noise(self):
        wav = require("noisy_snr5.wav")
        _, _, energy_f1 = run_engine("unimrcp_vad", wav)
        neural_f1s = []
        for name in ("silero_vad", "ten_vad"):
            info = registry.discover()[name]
            if info.available:
                neural_f1s.append(run_engine(name, wav)[2])
        if not neural_f1s:
            pytest.skip("no neural engine available")
        assert max(neural_f1s) > energy_f1, (
            f"expected a neural engine to beat energy under noise: energy F1={energy_f1:.2f}, "
            f"neural F1={[round(f, 2) for f in neural_f1s]}"
        )

    @pytest.mark.parametrize("engine", ["silero_vad", "ten_vad"])
    def test_neural_stays_usable_at_mild_noise(self, engine):
        _, _, f1 = run_engine(engine, require("noisy_snr15.wav"))
        assert f1 > 0.6, f"{engine} F1 collapsed at 15 dB SNR (F1={f1:.2f})"
