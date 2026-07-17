"""rn_denoise — RNNoise (xiph) noise suppressor, via ctypes.

The vendored RNNoise "little" model behind the same in-place int16 contract
as df_enhance, shared verbatim with the unimrcp arf-recog-ten-vad plugin
(third_party/rnnoise_enh/rn_denoise.c). RNNoise is 48 kHz-native; the 8 kHz
session audio is upsampled x6 with a polyphase FIR, denoised, and decimated
back inside the C wrapper. Latency 288 samples (36 ms) at 8 kHz.

The `wet` mix bounds RNNoise's known weakness on narrowband telephony (the
fullband-trained model sometimes gates real speech): wet w caps the
worst-case attenuation at 20*log10(1-w) — -14 dB at the 0.8 default.
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

from server.enhance.base import AudioEnhancer
from server.native import lib_path
from server.vad.base import ParamSpec

LIB_PATH = lib_path("rnnoise_enh", "librnenhance")


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(LIB_PATH))
    p = ctypes.c_void_p
    lib.rn_denoise_create.restype = p
    lib.rn_denoise_create.argtypes = []
    lib.rn_denoise_destroy.restype = None
    lib.rn_denoise_destroy.argtypes = [p]
    lib.rn_denoise_init.restype = ctypes.c_int
    lib.rn_denoise_init.argtypes = [p, ctypes.c_uint]
    lib.rn_denoise_reset.restype = None
    lib.rn_denoise_reset.argtypes = [p]
    lib.rn_denoise_process.restype = None
    lib.rn_denoise_process.argtypes = [p, ctypes.POINTER(ctypes.c_int16),
                                       ctypes.c_size_t, ctypes.c_int]
    lib.rn_denoise_wet_set.restype = None
    lib.rn_denoise_wet_set.argtypes = [p, ctypes.c_double]
    lib.rn_denoise_vad_prob.restype = ctypes.c_double
    lib.rn_denoise_vad_prob.argtypes = [p]
    lib.rn_denoise_latency_samples.restype = ctypes.c_uint
    lib.rn_denoise_latency_samples.argtypes = [p]
    return lib


class Engine(AudioEnhancer):
    name = "rnnoise"
    display_name = "RNNoise (xiph, little model)"
    params = [
        ParamSpec("wet", "Wet mix", "float", 0.8, 0.0, 1.0, 0.05,
                  help="RNNoise çıkışı ile ham (gecikme-hizalı) sinyalin karışımı. "
                       "1.0 = saf RNNoise; model dar-bant telefon sesinde bazen gerçek "
                       "konuşmayı da bastırdığından 0.8 varsayılanı hasarı -14 dB ile "
                       "sınırlar."),
    ]

    _lib: ctypes.CDLL | None = None

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        if not LIB_PATH.exists():
            return False, (f"librnenhance not found — run `make build-c` "
                           f"(expected at {LIB_PATH})")
        try:
            cls._get_lib()
        except OSError as exc:
            return False, f"failed to load librnenhance: {exc}"
        return True, ""

    @classmethod
    def _get_lib(cls) -> ctypes.CDLL:
        if cls._lib is None:
            cls._lib = _load_lib()
        return cls._lib

    def __init__(self, sample_rate: int, params: dict[str, Any] | None = None):
        super().__init__(sample_rate, params)
        lib = self._get_lib()
        self._c = lib
        self._rn = lib.rn_denoise_create()
        if not self._rn:
            raise MemoryError("rn_denoise_create failed")
        if lib.rn_denoise_init(self._rn, sample_rate) != 0:
            lib.rn_denoise_destroy(self._rn)
            self._rn = None
            raise ValueError(f"rn_denoise_init rejected rate {sample_rate}")
        self._apply_config()

    def _apply_config(self) -> None:
        self._c.rn_denoise_wet_set(self._rn, self.config["wet"])

    def process(self, frame: np.ndarray, is_speech: bool) -> np.ndarray:
        buf = np.ascontiguousarray(frame, dtype=np.int16).copy()
        ptr = buf.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        self._c.rn_denoise_process(self._rn, ptr, len(buf), 1 if is_speech else 0)
        return buf

    @property
    def vad_prob(self) -> float:
        return float(self._c.rn_denoise_vad_prob(self._rn))

    @property
    def latency_samples(self) -> int:
        return int(self._c.rn_denoise_latency_samples(self._rn))

    def reset(self) -> None:
        self._c.rn_denoise_reset(self._rn)

    def close(self) -> None:
        if getattr(self, "_rn", None):
            self._c.rn_denoise_destroy(self._rn)
            self._rn = None

    def __del__(self) -> None:
        self.close()
