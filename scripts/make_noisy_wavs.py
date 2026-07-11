"""Mix clean speech with background noise at controlled SNRs.

Produces noise-robustness fixtures: the same spoken words as speech.wav, but
with real ambient noise (MS-SNSD, if downloaded via scripts/fetch_noise.sh)
or a synthetic babble fallback laid under the whole clip at a few SNRs. The
ground-truth speech regions are unchanged — noise is everywhere, speech only
where it was — so the fixtures test how well each engine rejects noise.

    make wavs        # regenerates these
    uv run python scripts/make_noisy_wavs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.audio.wav_io import load_wav, save_wav  # noqa: E402

RATE = 8000
FIXTURES = REPO_ROOT / "tests" / "fixtures"
NOISE_DIR = REPO_ROOT / "data" / "noise"
SNRS_DB = [15, 5]  # mild, heavy


def synthetic_babble(n: int, rng: np.random.Generator) -> np.ndarray:
    """Speech-shaped noise with a syllabic amplitude rhythm — a stand-in for
    crowd babble when no real recording was downloaded."""
    white = rng.standard_normal(n)
    # 1/f-ish shaping so it has a speech-like tilt rather than flat white hiss
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    shape = np.ones_like(freqs)
    shape[1:] = 1.0 / np.sqrt(freqs[1:] / 300.0 + 1.0)  # emphasize 300-3400 Hz band
    shape[freqs < 150] *= 0.3
    shape[freqs > 3400] *= 0.4
    colored = np.fft.irfft(spectrum * shape, n)
    t = np.arange(n) / RATE
    # overlapping syllable envelopes at a few rates -> "many talkers"
    env = sum(0.5 + 0.5 * np.sin(2 * np.pi * r * t + rng.uniform(0, 6.28)) for r in (3.1, 4.7, 2.3)) / 3
    return colored * env


def load_noise(n: int, rng: np.random.Generator) -> tuple[np.ndarray, str]:
    """Real MS-SNSD noise if available (tiled/cropped to length), else babble.

    Prefers a Babble recording — overlapping crowd voices are the "background
    chatter" the user asked for and the hardest case for an energy detector.
    """
    files = sorted(NOISE_DIR.glob("*.wav")) if NOISE_DIR.is_dir() else []
    if files:
        babble = [f for f in files if "babble" in f.name.lower()]
        chosen = babble[0] if babble else files[0]
        noise = load_wav(chosen, RATE).astype(np.float64)
        if len(noise) < n:
            noise = np.tile(noise, int(np.ceil(n / len(noise))))
        start = rng.integers(0, max(1, len(noise) - n))
        return noise[start : start + n], chosen.name
    return synthetic_babble(n, rng), "synthetic-babble"


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, regions: list[dict], snr_db: float) -> np.ndarray:
    """speech + scaled noise so speech-region SNR equals snr_db."""
    speech = speech.astype(np.float64)
    mask = np.zeros(len(speech), dtype=bool)
    for r in regions:
        mask[int(r["start_ms"] * RATE / 1000) : int(r["end_ms"] * RATE / 1000)] = True
    speech_power = np.mean(speech[mask] ** 2) if mask.any() else np.mean(speech**2)
    noise_power = np.mean(noise**2) or 1.0
    gain = np.sqrt(speech_power / (noise_power * 10 ** (snr_db / 10)))
    return np.clip(speech + noise * gain, -32768, 32767).astype(np.int16)


def make_noisy_fixtures() -> list[Path]:
    clean_path = FIXTURES / "speech.wav"
    if not clean_path.exists():
        raise SystemExit("tests/fixtures/speech.wav missing — run scripts/make_test_wavs.py first")
    speech = load_wav(clean_path, RATE)
    regions = json.loads((FIXTURES / "speech.json").read_text())["speech_regions"]
    rng = np.random.default_rng(2024)
    noise, source = load_noise(len(speech), rng)

    written = []
    for snr in SNRS_DB:
        mixed = mix_at_snr(speech, noise, regions, snr)
        out = FIXTURES / f"noisy_snr{snr}.wav"
        save_wav(out, mixed, RATE)
        (out.with_suffix(".json")).write_text(
            json.dumps({"rate": RATE, "snr_db": snr, "noise_source": source, "speech_regions": regions}, indent=2)
        )
        written.append(out)
        print(f"{out.name}: SNR {snr} dB, noise={source}, {len(mixed) / RATE:.1f}s")
    return written


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    make_noisy_fixtures()
