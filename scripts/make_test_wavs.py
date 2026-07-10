"""Generate test WAV fixtures with known speech/silence boundaries.

- pattern1.wav: synthetic energy pattern (deterministic) for the unimrcp
  energy detector: silence / 2s "speech" / silence / 1s burst / silence.
- speech.wav: real synthesized speech via macOS `say` (for the neural
  engines, which ignore synthetic noise), embedded between silences.

Each file gets a .json sidecar with the ground-truth regions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.audio.wav_io import load_wav, save_wav  # noqa: E402

RATE = 8000
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _seconds(n: float) -> int:
    return int(n * RATE)


def _speech_like(duration_s: float, rng: np.random.Generator, amplitude: float = 6000.0) -> np.ndarray:
    """Noise with a syllable-ish 4 Hz envelope — loud enough for an energy
    detector, not real speech (neural engines are tested with speech.wav)."""
    n = _seconds(duration_s)
    t = np.arange(n) / RATE
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 4.0 * t)
    carrier = np.sin(2 * np.pi * 300.0 * t) * 0.5 + rng.standard_normal(n) * 0.5
    return (carrier * envelope * amplitude).clip(-32767, 32767).astype(np.int16)


def _near_silence(duration_s: float, rng: np.random.Generator) -> np.ndarray:
    # mean-abs must stay below the default level_threshold (2)
    return rng.integers(-1, 2, size=_seconds(duration_s)).astype(np.int16)


def make_pattern1() -> None:
    rng = np.random.default_rng(42)
    parts = [
        ("silence", _near_silence(1.0, rng)),
        ("speech", _speech_like(2.0, rng)),
        ("silence", _near_silence(1.0, rng)),
        ("speech", _speech_like(1.0, rng)),
        ("silence", _near_silence(1.0, rng)),
    ]
    audio = np.concatenate([p[1] for p in parts])
    regions, pos = [], 0
    for kind, samples in parts:
        if kind == "speech":
            regions.append({"start_ms": pos * 1000.0 / RATE, "end_ms": (pos + len(samples)) * 1000.0 / RATE})
        pos += len(samples)
    save_wav(FIXTURES / "pattern1.wav", audio, RATE)
    (FIXTURES / "pattern1.json").write_text(json.dumps({"rate": RATE, "speech_regions": regions}, indent=2))
    print(f"pattern1.wav: {len(audio) / RATE:.1f}s, speech at {regions}")


def make_speech() -> None:
    text = "Voice activity detection test. One, two, three. This is a longer sentence for the detectors."
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "say.aiff"
        wav = Path(tmp) / "say.wav"
        subprocess.run(["say", "-o", str(aiff), text], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", str(aiff), str(wav)],
            check=True,
        )
        spoken = load_wav(wav, RATE)

    rng = np.random.default_rng(7)
    lead, tail = _near_silence(1.0, rng), _near_silence(1.5, rng)
    audio = np.concatenate([lead, spoken, tail])

    # ground truth from the say-clip's own energy (10 ms mean-abs > 50)
    frame = RATE // 100
    trimmed = spoken[: len(spoken) // frame * frame].reshape(-1, frame)
    active = np.abs(trimmed.astype(np.int32)).mean(axis=1) > 50
    idx = np.flatnonzero(active)
    regions = []
    if len(idx):
        start_ms = (len(lead) + idx[0] * frame) * 1000.0 / RATE
        end_ms = (len(lead) + (idx[-1] + 1) * frame) * 1000.0 / RATE
        regions.append({"start_ms": start_ms, "end_ms": end_ms})
    save_wav(FIXTURES / "speech.wav", audio, RATE)
    (FIXTURES / "speech.json").write_text(json.dumps({"rate": RATE, "speech_regions": regions}, indent=2))
    print(f"speech.wav: {len(audio) / RATE:.1f}s, speech at {regions}")


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    make_pattern1()
    make_speech()
