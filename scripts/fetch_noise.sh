#!/bin/sh
# Download a few real ambient-noise recordings from Microsoft's MS-SNSD
# (MIT licensed) for the noise-robustness fixtures. These are the "background
# sounds" mixed under clean speech at controlled SNRs by make_noisy_wavs.py.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOISE_DIR="$REPO_ROOT/data/noise"
mkdir -p "$NOISE_DIR"

BASE="https://raw.githubusercontent.com/microsoft/MS-SNSD/master/noise_test"
# a spread of real ambient noises. Babble (overlapping voices) is the classic
# "background chatter"; the rest give variety for the sample library and for
# eyeballing engines under different noise types.
FILES="Babble_1.wav Babble_2.wav AirConditioner_1.wav AirConditioner_2.wav \
AirportAnnouncements_1.wav CopyMachine_1.wav Munching_1.wav Neighbor_1.wav \
ShuttingDoor_1.wav Typing_1.wav VacuumCleaner_1.wav Babble_3.wav"

for f in $FILES; do
    dest="$NOISE_DIR/$f"
    if [ -f "$dest" ]; then
        echo "$f already present"
        continue
    fi
    echo "downloading $f ..."
    if ! curl -sSfL -o "$dest" "$BASE/$f"; then
        echo "WARNING: could not download $f (continuing; synthetic fallback will be used)" >&2
        rm -f "$dest"
    fi
done

echo "noise files in $NOISE_DIR:"
ls -1 "$NOISE_DIR" 2>/dev/null || echo "  (none — make_noisy_wavs.py will synthesize babble)"
