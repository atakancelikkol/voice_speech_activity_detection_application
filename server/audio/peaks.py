"""Min/max peak pairs per fixed-size bin, for waveform rendering."""

from __future__ import annotations

import numpy as np


class PeaksBinner:
    def __init__(self, bin_samples: int = 80):  # 10 ms @ 8 kHz
        self.bin_samples = bin_samples
        self._carry = np.empty(0, dtype=np.int16)

    def feed(self, pcm: np.ndarray) -> list[tuple[int, int]]:
        data = np.concatenate([self._carry, np.asarray(pcm, dtype=np.int16)])
        n_bins = len(data) // self.bin_samples
        self._carry = data[n_bins * self.bin_samples :]
        if not n_bins:
            return []
        bins = data[: n_bins * self.bin_samples].reshape(n_bins, self.bin_samples)
        return list(zip(bins.min(axis=1).tolist(), bins.max(axis=1).tolist()))
