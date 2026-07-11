"""Build a library of noisy speech recordings under data/samples/ for
interactive testing (pick them from the app's "WAV file…" button).

Clean speech is synthesized with macOS `say` (several different sentences),
then each clip is mixed with a real MS-SNSD ambient-noise recording
(data/noise/, via scripts/fetch_noise.sh) at a chosen SNR. Filenames encode
the content so you can tell them apart in Finder, e.g.:

    speech1_babble_10dB.wav
    speech2_airconditioner_15dB.wav

Each file gets a .json sidecar with the ground-truth speech regions (noise
is everywhere; speech only where the words are).

    make samples      # regenerate
    uv run python scripts/make_sample_library.py
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
NOISE_DIR = REPO_ROOT / "data" / "noise"
SAMPLES_DIR = REPO_ROOT / "data" / "samples"

# a few different utterances so the library isn't the same words every time
SENTENCES = {
    "speech1": "Voice activity detection test. One, two, three. This is a longer sentence for the detectors.",
    "speech2": "The quick brown fox jumps over the lazy dog. Please leave a message after the tone.",
    "speech3": "Merhaba, bugün hava çok güzel. Lütfen sinyal sesinden sonra konuşun.",
}

# (noise-file substring, SNR in dB) pairs; the SNR mix determines how hard it is.
# a spread from mild (18 dB) to heavy (3 dB), leaning toward mild.
PLAN = [
    ("speech1", "Babble_1", 15),
    ("speech1", "Babble_2", 5),
    ("speech2", "AirConditioner_1", 18),
    ("speech2", "AirportAnnouncements_1", 10),
    ("speech3", "Babble_3", 12),
    ("speech3", "CopyMachine_1", 15),
    ("speech1", "Munching_1", 12),
    ("speech2", "Neighbor_1", 8),
    ("speech3", "ShuttingDoor_1", 15),
    ("speech1", "Typing_1", 18),
    ("speech2", "VacuumCleaner_1", 10),
    ("speech3", "AirConditioner_2", 6),
]


def synth_speech(text: str) -> tuple[np.ndarray, list[dict]]:
    """macOS `say` -> 8 kHz mono, framed by 1 s / 1.5 s silences, with
    ground-truth regions from the spoken clip's own energy."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "s.aiff", Path(tmp) / "s.wav"
        subprocess.run(["say", "-o", str(aiff), text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", str(aiff), str(wav)], check=True)
        spoken = load_wav(wav, RATE)

    rng = np.random.default_rng(len(text))
    lead = rng.integers(-1, 2, size=RATE).astype(np.int16)
    tail = rng.integers(-1, 2, size=int(1.5 * RATE)).astype(np.int16)
    audio = np.concatenate([lead, spoken, tail])

    frame = RATE // 100
    trimmed = spoken[: len(spoken) // frame * frame].reshape(-1, frame)
    active = np.abs(trimmed.astype(np.int32)).mean(axis=1) > 50
    idx = np.flatnonzero(active)
    regions = []
    if len(idx):
        regions.append(
            {
                "start_ms": (len(lead) + idx[0] * frame) * 1000.0 / RATE,
                "end_ms": (len(lead) + (idx[-1] + 1) * frame) * 1000.0 / RATE,
            }
        )
    return audio, regions


def find_noise(substring: str) -> Path | None:
    matches = sorted(NOISE_DIR.glob(f"{substring}*.wav")) if NOISE_DIR.is_dir() else []
    return matches[0] if matches else None


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, regions: list[dict], snr_db: float) -> np.ndarray:
    speech = speech.astype(np.float64)
    if len(noise) < len(speech):
        noise = np.tile(noise, int(np.ceil(len(speech) / len(noise))))
    noise = noise[: len(speech)].astype(np.float64)
    mask = np.zeros(len(speech), bool)
    for r in regions:
        mask[int(r["start_ms"] * RATE / 1000) : int(r["end_ms"] * RATE / 1000)] = True
    speech_power = np.mean(speech[mask] ** 2) if mask.any() else np.mean(speech**2)
    noise_power = np.mean(noise**2) or 1.0
    gain = np.sqrt(speech_power / (noise_power * 10 ** (snr_db / 10)))
    return np.clip(speech + noise * gain, -32768, 32767).astype(np.int16)


def main() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    speech_cache: dict[str, tuple[np.ndarray, list[dict]]] = {}
    written = 0
    for speech_key, noise_sub, snr in PLAN:
        noise_path = find_noise(noise_sub)
        if noise_path is None:
            print(f"skip {noise_sub}: not downloaded (run scripts/fetch_noise.sh)")
            continue
        if speech_key not in speech_cache:
            speech_cache[speech_key] = synth_speech(SENTENCES[speech_key])
        speech, regions = speech_cache[speech_key]
        mixed = mix_at_snr(speech, load_wav(noise_path, RATE), regions, snr)
        name = f"{speech_key}_{noise_sub.split('_')[0].lower()}_{snr}dB"
        save_wav(SAMPLES_DIR / f"{name}.wav", mixed, RATE)
        (SAMPLES_DIR / f"{name}.json").write_text(
            json.dumps(
                {"rate": RATE, "snr_db": snr, "noise_source": noise_path.name, "speech_regions": regions},
                indent=2,
            )
        )
        written += 1
        print(f"{name}.wav  ({len(mixed) / RATE:.1f}s, noise={noise_path.name})")
    print(f"\n{written} samples written to {SAMPLES_DIR}")


if __name__ == "__main__":
    main()
