"""Shared offline analysis: run one engine over a whole PCM buffer and
serialize its result the same way a live call does. Used by the re-analyze
endpoint (tune params, re-apply to an existing recording) and reused by the
live pipeline for score gridding so both stay consistent."""

from __future__ import annotations

import numpy as np

from server.vad.base import VadEngine
from server.vad.runner import SOURCE_RATE, EngineRunner, TimedScore

SCORE_GRID_MS = 10
CHUNK_MS = 20  # feed in 20 ms blocks, mirroring the RTP packet cadence


def grid_scores(scores: list[TimedScore], duration_ms: float, grid_ms: int = SCORE_GRID_MS) -> dict:
    """Down-sample per-frame scores onto a fixed grid for compact storage."""
    n = int(duration_ms // grid_ms) + 1
    values = np.zeros(n, dtype=np.float32)
    for s in scores:
        lo = int(s.t_ms // grid_ms)
        hi = min(n, int((s.t_ms + s.frame_ms) // grid_ms) + 1)
        values[lo:hi] = s.score
    return {"t0_ms": 0, "dt_ms": grid_ms, "values": [round(float(v), 4) for v in values]}


def analyze_pcm(engine: VadEngine, pcm: np.ndarray, config: dict | None = None) -> dict:
    """Run one engine over an 8 kHz int16 buffer; return the same per-engine
    payload shape the live pipeline persists (config/segments/events/scores)."""
    runner = EngineRunner(engine)
    all_scores: list[TimedScore] = []
    chunk = SOURCE_RATE * CHUNK_MS // 1000
    for start in range(0, len(pcm), chunk):
        all_scores.extend(runner.feed(pcm[start : start + chunk]))
    segments = runner.finalize()
    duration_ms = len(pcm) * 1000.0 / SOURCE_RATE
    return {
        "config": config or {},
        "axis": engine.score_axis(engine.config),
        "segments": [seg.as_dict() for seg in segments],
        "events": [{"kind": e.kind.value, "at_ms": round(e.at_ms, 1)} for e in runner.events],
        "scores": grid_scores(all_scores, duration_ms),
    }


def reanalyze_session(
    store, engine_manager, session_id: str, engine_names: list[str] | None
) -> dict:
    """Re-run engines over an existing (raw) recording with the engine
    manager's current params, overwriting only those engines' results.

    Engines always analyze the RAW recording — same as UniMRCP, where the VAD
    runs on the untouched audio and the enhancer only cleans what is streamed to
    the recognizer (STT), never the detector's input. To hear the enhanced audio
    the recognizer would receive, use the /enhanced.wav endpoint; it does not
    change any engine's segments.

    engine_names=None means "every currently enabled engine".
    """
    from server.audio.wav_io import load_wav

    session = store.read_session(session_id)
    names = engine_names if engine_names is not None else list(engine_manager.active_configs())
    unknown = [n for n in names if n not in engine_manager.infos]
    if unknown:
        raise ValueError(f"unknown engine(s): {', '.join(unknown)}")
    unavailable = [n for n in names if not engine_manager.infos[n].available]
    if unavailable:
        raise ValueError(f"engine(s) unavailable: {', '.join(unavailable)}")

    pcm = load_wav(store.audio_path(session_id), SOURCE_RATE)

    session.setdefault("engines", {})
    for name in names:
        engine = engine_manager.instantiate(name)
        try:
            session["engines"][name] = analyze_pcm(engine, pcm, engine_manager.config_of(name))
        finally:
            engine.close()
    store.write_session(session_id, session)
    return session
