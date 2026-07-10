"""EngineRunner adapter properties: resampling/rebuffering must keep the
engine's frame timestamps monotonic, gapless, and aligned with source time."""

import numpy as np

from server.vad.base import AudioFormat, FrameScore, VadEngine
from server.vad.runner import SOURCE_RATE, EngineRunner


class SpyEngine(VadEngine):
    name = "spy"
    display_name = "Spy"
    params = []

    def __init__(self, fmt: AudioFormat):
        super().__init__({})
        self._fmt = fmt
        self.frames: list[tuple[float, int]] = []

    @property
    def input_format(self) -> AudioFormat:
        return self._fmt

    def process(self, frame: np.ndarray, frame_start_ms: float) -> FrameScore:
        assert len(frame) == self._fmt.frame_samples
        assert frame.dtype == np.int16
        self.frames.append((frame_start_ms, len(frame)))
        return FrameScore(score=0.0, raw=0.0)


def feed_seconds(runner: EngineRunner, seconds: float, chunk_ms: int = 20) -> None:
    rng = np.random.default_rng(0)
    total = int(SOURCE_RATE * seconds)
    chunk = SOURCE_RATE * chunk_ms // 1000
    pcm = (rng.standard_normal(total) * 1000).astype(np.int16)
    for start in range(0, total, chunk):
        runner.feed(pcm[start : start + chunk])


def assert_gapless(engine: SpyEngine) -> None:
    fmt = engine.input_format
    for i, (t_ms, _) in enumerate(engine.frames):
        assert abs(t_ms - i * fmt.frame_ms) < 1e-6, f"frame {i} at {t_ms}, expected {i * fmt.frame_ms}"


def test_native_rate_frames_are_gapless():
    engine = SpyEngine(AudioFormat(8000, 80))
    runner = EngineRunner(engine)
    feed_seconds(runner, 2.0)
    runner.finalize()
    assert_gapless(engine)
    # 2 s of 10 ms frames
    assert len(engine.frames) == 200


def test_resampled_frames_cover_input_duration():
    engine = SpyEngine(AudioFormat(16000, 512))
    runner = EngineRunner(engine)
    feed_seconds(runner, 2.0)
    runner.finalize()
    assert_gapless(engine)
    covered_ms = len(engine.frames) * engine.input_format.frame_ms
    # resampler flush must deliver (almost) the whole input duration
    assert abs(covered_ms - 2000.0) <= 2 * engine.input_format.frame_ms


def test_odd_chunk_sizes_do_not_break_framing():
    engine = SpyEngine(AudioFormat(16000, 256))
    runner = EngineRunner(engine)
    rng = np.random.default_rng(1)
    remaining = SOURCE_RATE  # 1 s in irregular chunks
    while remaining > 0:
        n = min(remaining, int(rng.integers(1, 500)))
        runner.feed((rng.standard_normal(n) * 500).astype(np.int16))
        remaining -= n
    runner.finalize()
    assert_gapless(engine)
    covered_ms = len(engine.frames) * engine.input_format.frame_ms
    assert abs(covered_ms - 1000.0) <= 2 * engine.input_format.frame_ms


def test_position_tracks_source_samples():
    engine = SpyEngine(AudioFormat(16000, 512))
    runner = EngineRunner(engine)
    runner.feed(np.zeros(1234, dtype=np.int16))
    assert abs(runner.position_ms - 1234 * 1000.0 / SOURCE_RATE) < 1e-6
