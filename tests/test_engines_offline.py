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
        assert noinput and abs(noinput[0].event.at_ms - 1000.0) <= 20.0

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
