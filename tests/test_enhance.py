"""Audio enhancer: the arf_enhance plugin, the manager, and that it actually
changes the audio (and reduces the noise floor with denoise on)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from server.enhance import registry
from server.enhance.base import SpeechHint
from server.enhance.manager import EnhancerManager

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = Path(__file__).parents[1] / "data" / "samples" / "speech3_airconditioner_6dB.wav"


def enhance_stream(params, wav):
    info = registry.discover()["arf_enhance"]
    if not info.available:
        pytest.skip(info.reason)
    from server.audio.wav_io import load_wav

    pcm = load_wav(wav, 8000)
    enh = registry.create(info, 8000, params)
    hint = SpeechHint(8000)
    out = [enh.process(pcm[i : i + 160], hint.update(pcm[i : i + 160])) for i in range(0, len(pcm), 160)]
    enh.close()
    return pcm, np.concatenate(out)


class TestArfEnhance:
    def test_available_with_18_params(self):
        info = registry.discover()["arf_enhance"]
        if not info.available:
            pytest.skip(info.reason)
        assert len(info.params) == 18
        assert {p.name for p in info.params} >= {"denoise", "hp_fc", "shelf_gain", "leveler"}

    def test_process_preserves_length_and_dtype(self):
        raw, out = enhance_stream(None, FIXTURES / "speech.wav")
        assert len(out) == len(raw)  # in-place, sample-for-sample
        assert out.dtype == np.int16

    def test_enhancer_changes_the_audio(self):
        raw, out = enhance_stream(None, FIXTURES / "speech.wav")
        n = min(len(raw), len(out))
        # the default chain (hp/shelf/leveler) must actually alter the signal
        assert not np.array_equal(raw[:n], out[:n])

    def test_denoise_lowers_the_noise_floor(self):
        if not SAMPLE.exists():
            pytest.skip("noisy sample missing — run `make samples`")
        regions = json.loads(SAMPLE.with_suffix(".json").read_text())["speech_regions"]
        lead = int(regions[0]["start_ms"] * 8)  # pure-noise lead-in

        def noise_rms(x):
            seg = x[: lead if lead > 800 else 800]
            return float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))

        _, off = enhance_stream({"denoise": False, "leveler": False, "hp_auto": False, "hp_fc": 0}, SAMPLE)
        _, on = enhance_stream({"denoise": True, "oversub": 2.0, "leveler": False, "hp_auto": False, "hp_fc": 0}, SAMPLE)
        # with everything else neutralized, denoise must reduce the noise floor
        assert noise_rms(on) < noise_rms(off) * 0.95


class TestEnhancerManager:
    def test_off_by_default(self):
        m = EnhancerManager()
        assert m.active_name() is None

    def test_enable_and_configure(self):
        m = EnhancerManager()
        if not m.infos["arf_enhance"].available:
            pytest.skip("arf_enhance unavailable")
        m.configure("arf_enhance", enabled=True, params={"denoise": True})
        assert m.active_name() == "arf_enhance"
        assert m.config_of("arf_enhance")["denoise"] is True

    def test_unknown_param_rejected(self):
        m = EnhancerManager()
        if not m.infos["arf_enhance"].available:
            pytest.skip("arf_enhance unavailable")
        with pytest.raises(ValueError, match="unknown parameter"):
            m.configure("arf_enhance", params={"nope": 1})

    def test_instantiate_active_returns_none_when_disabled(self):
        m = EnhancerManager()
        assert m.instantiate_active(8000) is None
