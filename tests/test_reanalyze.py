"""Re-analyze: tune params, re-apply to an existing recording offline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from server.analysis import analyze_pcm, reanalyze_session
from server.audio.wav_io import load_wav
from server.engines_state import EngineManager
from server.sessions.store import SessionStore
from server.vad import registry
from server.vad.runner import SOURCE_RATE

FIXTURE = Path(__file__).parent / "fixtures" / "speech.wav"


@pytest.fixture
def seeded_store(tmp_path):
    """A store with one session (recording + a session.json from a first run)."""
    store = SessionStore(tmp_path)
    session_id, session_dir = store.new_session_dir("call-abc")
    shutil.copy(FIXTURE, session_dir / "audio.wav")

    pcm = load_wav(FIXTURE, SOURCE_RATE)
    manager = EngineManager()
    engines = {}
    for name, info in manager.infos.items():
        if info.available:
            engines[name] = analyze_pcm(registry.create(info), pcm, manager.config_of(name))
    payload = {
        "id": session_id,
        "duration_ms": len(pcm) * 1000.0 / SOURCE_RATE,
        "sample_rate": SOURCE_RATE,
        "peaks": {"t0_ms": 0, "dt_ms": 10, "values": []},
        "engines": engines,
    }
    store.write_session(session_id, payload)
    return store, session_id, manager


def test_analyze_pcm_matches_unimrcp_default():
    pcm = load_wav(FIXTURE, SOURCE_RATE)
    info = registry.discover()["unimrcp_vad"]
    if not info.available:
        pytest.skip(info.reason)
    result = analyze_pcm(registry.create(info), pcm, {"level_threshold": 2})
    assert result["segments"], "expected speech detected"
    assert result["config"]["level_threshold"] == 2


def test_reanalyze_changes_segments_when_threshold_raised(seeded_store):
    store, session_id, manager = seeded_store
    if not manager.infos["unimrcp_vad"].available:
        pytest.skip("unimrcp_vad unavailable")
    before = store.read_session(session_id)["engines"]["unimrcp_vad"]["segments"]

    # a high energy threshold must detect strictly less speech
    manager.configure("unimrcp_vad", params={"level_threshold": 5000})
    updated = reanalyze_session(store, manager, session_id, ["unimrcp_vad"])
    after = updated["engines"]["unimrcp_vad"]["segments"]

    assert after != before
    assert updated["engines"]["unimrcp_vad"]["config"]["level_threshold"] == 5000
    total_after = sum(s["end_ms"] - s["start_ms"] for s in after)
    total_before = sum(s["end_ms"] - s["start_ms"] for s in before)
    assert total_after < total_before

    # persisted to disk
    on_disk = json.loads((store._session_dir(session_id) / "session.json").read_text())
    assert on_disk["engines"]["unimrcp_vad"]["config"]["level_threshold"] == 5000


def test_reanalyze_only_touches_named_engine(seeded_store):
    store, session_id, manager = seeded_store
    others = [n for n in manager.active_configs() if n != "unimrcp_vad"]
    if not others or not manager.infos["unimrcp_vad"].available:
        pytest.skip("need unimrcp_vad plus another engine")
    before_other = store.read_session(session_id)["engines"][others[0]]

    manager.configure("unimrcp_vad", params={"level_threshold": 5000})
    updated = reanalyze_session(store, manager, session_id, ["unimrcp_vad"])

    assert updated["engines"][others[0]] == before_other  # untouched


def test_reanalyze_all_defaults_to_enabled_engines(seeded_store):
    store, session_id, manager = seeded_store
    updated = reanalyze_session(store, manager, session_id, None)
    assert set(updated["engines"]) >= set(manager.active_configs())


def test_reanalyze_rejects_unknown_engine(seeded_store):
    store, session_id, manager = seeded_store
    with pytest.raises(ValueError, match="unknown engine"):
        reanalyze_session(store, manager, session_id, ["does_not_exist"])


def test_reanalyze_preserves_annotations(seeded_store):
    store, session_id, manager = seeded_store
    store.write_annotations(session_id, {"speech_regions": [{"start_ms": 1000, "end_ms": 2000}]})
    reanalyze_session(store, manager, session_id, None)
    assert store.read_annotations(session_id)["speech_regions"] == [{"start_ms": 1000, "end_ms": 2000}]


def test_reanalyze_scores_the_raw_recording(seeded_store):
    """UniMRCP-faithful: engines analyze the RAW recording, decoupled from the
    enhancer (which only feeds the /enhanced.wav preview). Re-analysis therefore
    reproduces a direct raw analyze_pcm and the session carries no enhancer
    field — enabling an enhancer can never flatter a VAD's result here."""
    store, session_id, manager = seeded_store
    if not manager.infos["unimrcp_vad"].available:
        pytest.skip("unimrcp_vad unavailable")

    pcm = load_wav(store.audio_path(session_id), SOURCE_RATE)
    engine = manager.instantiate("unimrcp_vad")
    try:
        raw = analyze_pcm(engine, pcm, manager.config_of("unimrcp_vad"))
    finally:
        engine.close()

    updated = reanalyze_session(store, manager, session_id, ["unimrcp_vad"])
    assert updated["engines"]["unimrcp_vad"]["segments"] == raw["segments"]
    assert "enhancer" not in updated  # the enhancer no longer touches the session
