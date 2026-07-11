# Noisy fixture provenance

`noisy_snr15.wav` and `noisy_snr5.wav` are clean speech (`speech.wav`, our own
macOS `say` synthesis) mixed with a background-noise recording from Microsoft's
**MS-SNSD** dataset (https://github.com/microsoft/MS-SNSD), which is licensed
under the **MIT License**, Copyright (c) Microsoft Corporation.

The `noise_source` field in each `.json` sidecar names the recording used
(e.g. `Babble_1.wav`). Regenerate with `make wavs` (downloads via
`scripts/fetch_noise.sh` if `data/noise/` is empty, else uses a synthetic
babble fallback).
