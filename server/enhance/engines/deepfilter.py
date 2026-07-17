"""df_enhance — DeepFilterNet-structured noise suppressor, via ctypes.

C port of the DeepFilterNet processing STRUCTURE (STFT -> ERB-band gain
stage -> "deep filtering" multi-frame Wiener on the low bins -> ISTFT) with
classical estimators in place of the neural networks. Shared verbatim with
the unimrcp arf-recog-ten-vad plugin (third_party/df_enhance/df_enhance.c);
sample-rate agnostic: 8 kHz telephony gets its own geometry (160/80, FFT 256,
~21 ERB bands, deep filtering up to 2 kHz) instead of a resampled 48 kHz one.
Latency 2 hops (20 ms).
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

from server.enhance.base import AudioEnhancer
from server.native import lib_path
from server.vad.base import ParamSpec

LIB_PATH = lib_path("df_enhance", "libdfenhance")


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(LIB_PATH))
    p = ctypes.c_void_p
    lib.df_enhance_create.restype = p
    lib.df_enhance_create.argtypes = []
    lib.df_enhance_destroy.restype = None
    lib.df_enhance_destroy.argtypes = [p]
    lib.df_enhance_init.restype = ctypes.c_int
    lib.df_enhance_init.argtypes = [p, ctypes.c_uint]
    lib.df_enhance_reset.restype = None
    lib.df_enhance_reset.argtypes = [p]
    lib.df_enhance_process.restype = None
    lib.df_enhance_process.argtypes = [p, ctypes.POINTER(ctypes.c_int16),
                                       ctypes.c_size_t, ctypes.c_int]
    for name in ("df_enhance_stage1_enable", "df_enhance_stage2_enable",
                 "df_enhance_spp_enable", "df_enhance_noise_hint_enable",
                 "df_enhance_bypass_set"):
        fn = getattr(lib, name)
        fn.restype = None
        fn.argtypes = [p, ctypes.c_int]
    for name in ("df_enhance_gain_floor_db_set", "df_enhance_gain_exponent_set",
                 "df_enhance_noise_bias_set", "df_enhance_df_cutoff_hz_set",
                 "df_enhance_df_alpha_max_set", "df_enhance_df_boost_max_db_set"):
        fn = getattr(lib, name)
        fn.restype = None
        fn.argtypes = [p, ctypes.c_double]
    for name, res in (("df_enhance_noise_level_db", ctypes.c_double),
                      ("df_enhance_mean_gain", ctypes.c_double),
                      ("df_enhance_latency_samples", ctypes.c_uint),
                      ("df_enhance_band_count", ctypes.c_uint),
                      ("df_enhance_nan_resets", ctypes.c_uint)):
        fn = getattr(lib, name)
        fn.restype = res
        fn.argtypes = [p]
    return lib


class Engine(AudioEnhancer):
    name = "df_enhance"
    display_name = "DeepFilter enhance (ERB + deep filtering)"
    params = [
        ParamSpec("stage2", "Deep filtering (stage 2)", "bool", True,
                  help="Düşük frekans bin'lerinde 5-frame'lik karmaşık multi-frame Wiener "
                       "filtresi (DFN'in deep filtering aşamasının klasik karşılığı). "
                       "Kapalıyken sadece ERB-band Wiener (stage 1) çalışır."),
        ParamSpec("gain_floor_db", "Gain floor", "float", -15.0, -30.0, 0.0, 1.0, "dB",
                  help="Stage-1 kazancının inebileceği taban. Daha negatif = daha agresif "
                       "gürültü bastırma, daha yüksek konuşma-bozulması riski."),
        ParamSpec("gain_exponent", "Wiener exponent", "float", 1.0, 0.5, 2.0, 0.1,
                  help="Wiener kazancının üssü. >1 = daha sert bastırma eğrisi, "
                       "<1 = daha yumuşak."),
        ParamSpec("spp", "SPP weighting", "bool", True,
                  help="Konuşma-varlık olasılığıyla kazanç harmanlama: konuşma "
                       "olasılığı düşük bandlar tabana çekilir (müzikal gürültüyü azaltır)."),
        ParamSpec("noise_bias", "Noise bias", "float", 1.3, 1.0, 2.5, 0.05,
                  help="Minimum-istatistik gürültü takibinin sistematik düşük tahminini "
                       "telafi eden çarpan. Yüksek = gürültü daha yüksek varsayılır."),
        ParamSpec("df_cutoff_hz", "DF cutoff", "int", 0, 0, 5000, 100, "Hz",
                  help="Deep filtering'in uygulandığı üst frekans (0 = otomatik: "
                       "min(5000, rate/4) -> 8 kHz'de 2 kHz)."),
        ParamSpec("df_alpha_max", "DF blend max", "float", 0.8, 0.0, 1.0, 0.05,
                  help="Stage-2 çıkışının stage-1 ile harmanlanma üst sınırı "
                       "(0 = stage 2 fiilen kapalı)."),
        ParamSpec("df_boost_max_db", "DF boost clamp", "float", 6.0, 0.0, 12.0, 0.5, "dB",
                  help="Deep filter'ın stage-1 genliğine göre uygulayabileceği en fazla "
                       "yükseltme; kestirim hatalarını sınırlar."),
        ParamSpec("noise_hint", "Use VAD hint", "bool", True,
                  help="Konuşma-dışı frame ipucuyla gürültü öğrenimini hızlandır."),
    ]

    _lib: ctypes.CDLL | None = None

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        if not LIB_PATH.exists():
            return False, (f"libdfenhance not found — run `make build-c` "
                           f"(expected at {LIB_PATH})")
        try:
            cls._get_lib()
        except OSError as exc:
            return False, f"failed to load libdfenhance: {exc}"
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
        self._de = lib.df_enhance_create()
        if not self._de:
            raise MemoryError("df_enhance_create failed")
        if lib.df_enhance_init(self._de, sample_rate) != 0:
            lib.df_enhance_destroy(self._de)
            self._de = None
            raise ValueError(f"df_enhance_init rejected rate {sample_rate}")
        self._apply_config()

    def _apply_config(self) -> None:
        c, de, cfg = self._c, self._de, self.config
        c.df_enhance_stage2_enable(de, int(cfg["stage2"]))
        c.df_enhance_gain_floor_db_set(de, cfg["gain_floor_db"])
        c.df_enhance_gain_exponent_set(de, cfg["gain_exponent"])
        c.df_enhance_spp_enable(de, int(cfg["spp"]))
        c.df_enhance_noise_bias_set(de, cfg["noise_bias"])
        c.df_enhance_df_cutoff_hz_set(de, float(cfg["df_cutoff_hz"]))
        c.df_enhance_df_alpha_max_set(de, cfg["df_alpha_max"])
        c.df_enhance_df_boost_max_db_set(de, cfg["df_boost_max_db"])
        c.df_enhance_noise_hint_enable(de, int(cfg["noise_hint"]))

    def process(self, frame: np.ndarray, is_speech: bool) -> np.ndarray:
        buf = np.ascontiguousarray(frame, dtype=np.int16).copy()
        ptr = buf.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        self._c.df_enhance_process(self._de, ptr, len(buf), 1 if is_speech else 0)
        return buf

    @property
    def noise_level_db(self) -> float:
        return float(self._c.df_enhance_noise_level_db(self._de))

    @property
    def latency_samples(self) -> int:
        return int(self._c.df_enhance_latency_samples(self._de))

    def reset(self) -> None:
        self._c.df_enhance_reset(self._de)

    def close(self) -> None:
        if getattr(self, "_de", None):
            self._c.df_enhance_destroy(self._de)
            self._de = None

    def __del__(self) -> None:
        self.close()
