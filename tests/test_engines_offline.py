"""Offline engine tests over the generated fixtures.

Run `uv run python scripts/make_test_wavs.py` first (or `make wavs`);
fixtures are committed, so this is only needed after changing the script.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from server.audio.wav_io import load_wav
from server.vad import registry
from server.vad.runner import SOURCE_RATE, EngineRunner

FIXTURES = Path(__file__).parent / "fixtures"


def run_engine(name: str, wav: Path, params: dict | None = None):
    infos = registry.discover()
    info = infos[name]
    if not info.available:
        pytest.skip(f"{name} unavailable: {info.reason}")
    runner = EngineRunner(registry.create(info, params))
    pcm = load_wav(wav, SOURCE_RATE)
    chunk = SOURCE_RATE * 20 // 1000
    for start in range(0, len(pcm), chunk):
        runner.feed(pcm[start : start + chunk])
    return runner.finalize(), runner


def expected_regions(wav: Path) -> list[dict]:
    return json.loads(wav.with_suffix(".json").read_text())["speech_regions"]


def assert_matches(segments, regions, tolerance_ms: float):
    assert len(segments) == len(regions), f"expected {len(regions)} segments, got {segments}"
    for seg, region in zip(segments, regions):
        assert abs(seg.start_ms - region["start_ms"]) <= tolerance_ms
        assert abs(seg.end_ms - region["end_ms"]) <= tolerance_ms


class TestUnimrcpVad:
    def test_pattern1_segments(self):
        wav = FIXTURES / "pattern1.wav"
        segments, _ = run_engine("unimrcp_vad", wav)
        # backdating brings starts within one transition of the truth
        assert_matches(segments, expected_regions(wav), tolerance_ms=350.0)

    def test_speech_segments(self):
        wav = FIXTURES / "speech.wav"
        segments, _ = run_engine("unimrcp_vad", wav)
        assert segments, "no speech detected in real speech fixture"
        regions = expected_regions(wav)
        # detector may split on pauses; the union must cover the spoken region
        assert abs(segments[0].start_ms - regions[0]["start_ms"]) <= 400.0
        assert abs(segments[-1].end_ms - regions[0]["end_ms"]) <= 400.0

    def test_noinput_event_on_silence(self):
        infos = registry.discover()
        info = infos["unimrcp_vad"]
        if not info.available:
            pytest.skip(info.reason)
        runner = EngineRunner(registry.create(info, {"noinput_timeout": 1000}))
        silence = np.zeros(SOURCE_RATE * 2, dtype=np.int16)  # 2 s
        scores = runner.feed(silence)
        noinput = [s for s in scores if s.event and s.event.kind.value == "noinput"]
        # exactly one report (the C detector re-fires every frame; the wrapper dedupes)
        assert len(noinput) == 1
        assert abs(noinput[0].event.at_ms - 1000.0) <= 20.0

    def test_score_curve_tracks_energy(self):
        wav = FIXTURES / "pattern1.wav"
        infos = registry.discover()
        info = infos["unimrcp_vad"]
        if not info.available:
            pytest.skip(info.reason)
        runner = EngineRunner(registry.create(info))
        pcm = load_wav(wav, SOURCE_RATE)
        scores = runner.feed(pcm)
        loud = [s.raw for s in scores if 1100 < s.t_ms < 2900]
        quiet = [s.raw for s in scores if s.t_ms < 900]
        assert min(loud) > max(quiet)


class TestArfVad:
    def test_pattern1_segments(self):
        # default config (libfvad gate ON): the strict open threshold adds
        # ~fvad_window*open_pct frames of onset delay, still within tolerance
        wav = FIXTURES / "pattern1.wav"
        segments, _ = run_engine("arf_vad", wav)
        assert_matches(segments, expected_regions(wav), tolerance_ms=350.0)

    def test_pattern1_segments_energy_only(self):
        # without the spectral gate the backdated onsets land on the truth
        wav = FIXTURES / "pattern1.wav"
        segments, _ = run_engine("arf_vad", wav, {"use_fvad": False})
        assert_matches(segments, expected_regions(wav), tolerance_ms=50.0)

    def test_speech_segments(self):
        wav = FIXTURES / "speech.wav"
        segments, _ = run_engine("arf_vad", wav)
        assert segments, "no speech detected in real speech fixture"
        regions = expected_regions(wav)
        # the fvad open gate only ever delays an onset, never fires early
        assert segments[0].start_ms >= regions[0]["start_ms"] - 50.0
        assert abs(segments[0].start_ms - regions[0]["start_ms"]) <= 500.0
        assert abs(segments[-1].end_ms - regions[0]["end_ms"]) <= 400.0

    def test_spec_bypass_recovers_fvad_vetoed_words(self):
        # With the strict open threshold, libfvad under-votes the short
        # "One, two, three" words after a pause and the segment is vetoed.
        # spec_bypass_snr lets unambiguously loud frames override the veto —
        # the deployed fix for exactly this failure mode ("evet" problem).
        wav = FIXTURES / "speech.wav"
        gated, _ = run_engine("arf_vad", wav)
        bypass, _ = run_engine("arf_vad", wav, {"spec_bypass_snr": 25.0})
        energy_only, _ = run_engine("arf_vad", wav, {"use_fvad": False})
        assert len(bypass) == len(energy_only) > len(gated)
        for seg_b, seg_e in zip(bypass, energy_only):
            assert abs(seg_b.start_ms - seg_e.start_ms) <= 50.0
            assert abs(seg_b.end_ms - seg_e.end_ms) <= 50.0

    def test_noinput_event_on_silence(self):
        infos = registry.discover()
        info = infos["arf_vad"]
        if not info.available:
            pytest.skip(info.reason)
        runner = EngineRunner(registry.create(info, {"noinput_timeout": 1000}))
        silence = np.zeros(SOURCE_RATE * 2, dtype=np.int16)  # 2 s
        scores = runner.feed(silence)
        noinput = [s for s in scores if s.event and s.event.kind.value == "noinput"]
        # the C detector re-fires once per timeout window; the wrapper dedupes
        assert len(noinput) == 1
        assert abs(noinput[0].event.at_ms - 1000.0) <= 20.0

    def test_snr_curve_separates_speech_from_silence(self):
        wav = FIXTURES / "pattern1.wav"
        infos = registry.discover()
        info = infos["arf_vad"]
        if not info.available:
            pytest.skip(info.reason)
        runner = EngineRunner(registry.create(info))
        pcm = load_wav(wav, SOURCE_RATE)
        scores = runner.feed(pcm)
        # raw is SNR in dB over the adaptive noise floor
        loud = [s.raw for s in scores if 1100 < s.t_ms < 2900]
        quiet = [s.raw for s in scores if 100 < s.t_ms < 900]
        assert min(loud) > max(quiet) + 20.0

    def test_bool_param_accepts_cli_strings(self):
        # CLI --param values arrive as strings; "0"/"false" must mean False
        infos = registry.discover()
        info = infos["arf_vad"]
        if not info.available:
            pytest.skip(info.reason)
        engine = registry.create(info, {"use_fvad": "0"})
        try:
            assert engine.config["use_fvad"] is False
        finally:
            engine.close()
        engine = registry.create(info, {"use_fvad": "true"})
        try:
            assert engine.config["use_fvad"] is True
        finally:
            engine.close()
