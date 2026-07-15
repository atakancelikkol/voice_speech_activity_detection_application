"""arf_enhance — the arf-recog-adaptive-vad audio enhancer, via ctypes.

STFT speech enhancer: spectral-subtraction denoise, adaptive high-pass
de-boom, high-shelf de-muffle, pumping-free leveler, soft-knee limiter.
Defaults are the recognizer engine's production chain
(arf-recog-adaptive-vad/src/arf_recog_engine.c): hp 120 Hz + auto, shelf
1800 Hz/+7 dB/Q0.707, leveler on (3000/3/200), AGC and denoise off, limiter
soft-knee. Works at 8 kHz and 16 kHz (arf_audio_enhance_init takes the rate).
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

from server.enhance.base import AudioEnhancer
from server.native import lib_path
from server.vad.base import ParamSpec

LIB_PATH = lib_path("arf_enhance", "libarfenhance")


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(LIB_PATH))
    p = ctypes.c_void_p
    lib.arf_audio_enhance_create.restype = p
    lib.arf_audio_enhance_create.argtypes = []
    lib.arf_audio_enhance_destroy.restype = None
    lib.arf_audio_enhance_destroy.argtypes = [p]
    lib.arf_audio_enhance_init.restype = None
    lib.arf_audio_enhance_init.argtypes = [p, ctypes.c_uint]
    lib.arf_audio_enhance_reset.restype = None
    lib.arf_audio_enhance_reset.argtypes = [p]
    for name in ("arf_audio_enhance_denoise_enable", "arf_audio_enhance_agc_enable",
                 "arf_audio_enhance_leveler_enable", "arf_audio_enhance_limiter_mode_set"):
        fn = getattr(lib, name)
        fn.restype = None
        fn.argtypes = [p, ctypes.c_int]
    for name in ("arf_audio_enhance_oversub_set", "arf_audio_enhance_floor_set",
                 "arf_audio_enhance_hp_set", "arf_audio_enhance_preemph_set",
                 "arf_audio_enhance_agc_target_set", "arf_audio_enhance_agc_max_gain_set",
                 "arf_audio_enhance_nonspeech_atten_set"):
        fn = getattr(lib, name)
        fn.restype = None
        fn.argtypes = [p, ctypes.c_double]
    lib.arf_audio_enhance_band_set.restype = None
    lib.arf_audio_enhance_band_set.argtypes = [p, ctypes.c_double, ctypes.c_double]
    lib.arf_audio_enhance_shelf_set.restype = None
    lib.arf_audio_enhance_shelf_set.argtypes = [p, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    lib.arf_audio_enhance_leveler_set.restype = None
    lib.arf_audio_enhance_leveler_set.argtypes = [p, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    lib.arf_audio_enhance_hp_auto_set.restype = None
    lib.arf_audio_enhance_hp_auto_set.argtypes = [p, ctypes.c_int, ctypes.c_double, ctypes.c_double]
    lib.arf_audio_enhance_process.restype = None
    lib.arf_audio_enhance_process.argtypes = [p, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.c_int]
    for name in ("arf_audio_enhance_noise_rms", "arf_audio_enhance_avg_denoise_gain",
                 "arf_audio_enhance_agc_gain"):
        fn = getattr(lib, name)
        fn.restype = ctypes.c_double
        fn.argtypes = [p]
    return lib


class Engine(AudioEnhancer):
    name = "arf_enhance"
    display_name = "arf enhance (denoise/EQ/leveler)"
    params = [
        # spectral-subtraction denoiser (off by default, like the engine)
        ParamSpec("denoise", "Denoise (spectral)", "bool", False,
                  help="Spectral-subtraction denoiser. Varsayılan kapalı, production recognizer "
                       "zinciriyle uyumlu."),
        ParamSpec("oversub", "Denoise over-subtraction", "float", 1.0, 0.0, 4.0, 0.1,
                  help="Tahmini gürültünün ne kadar agresif çıkarıldığı. Yüksek = daha çok "
                       "gürültü giderir ama 'musical' artefakt riski."),
        ParamSpec("floor_gain", "Denoise floor gain", "float", 0.15, 0.0, 1.0, 0.05,
                  help="Denoise sırasında her frekans bin'i için tutulan minimum kazanç, "
                       "böylece konuşma aşırı bastırılmaz."),
        # de-boom high-pass
        ParamSpec("hp_fc", "High-pass cutoff", "int", 120, 0, 500, 10, "Hz",
                  help="Bu frekansın altındaki düşük-frekans uğultuyu/boom'u kaldıran "
                       "high-pass kesim."),
        ParamSpec("hp_auto", "Adaptive high-pass", "bool", True,
                  help="High-pass kesim frekansının sabit kalmak yerine sinyale göre uyum "
                       "sağlamasına izin ver."),
        # de-muffle high-shelf
        ParamSpec("shelf_fc", "De-muffle shelf freq", "int", 1800, 500, 3800, 50, "Hz",
                  help="De-muffle high-shelf'in netlik için yüksekleri kaldırmaya başladığı "
                       "frekans."),
        ParamSpec("shelf_gain", "De-muffle shelf gain", "float", 7.0, 0.0, 12.0, 0.5, "dB",
                  help="High-shelf'in konuşmayı de-muffle etmek için yüksek frekanslara "
                       "uyguladığı kazanç."),
        ParamSpec("shelf_q", "De-muffle shelf Q", "float", 0.707, 0.3, 2.0, 0.05,
                  help="Shelf dikliği. Düşük Q = daha yumuşak, daha geniş geçiş."),
        ParamSpec("preemph", "Pre-emphasis", "float", 0.0, 0.0, 0.97, 0.01,
                  help="Yüksek frekansları artırarak sinyali parlatan pre-emphasis katsayısı "
                       "(0 = kapalı)."),
        # leveler (on) + legacy AGC (off) + non-speech duck
        ParamSpec("leveler", "Leveler", "bool", True,
                  help="Pompalamadan sesi bir hedefe doğru yumuşatan RMS leveler."),
        ParamSpec("leveler_target", "Leveler target RMS", "int", 3000, 500, 8000, 100,
                  help="Leveler'ın konuşmayı sürüklediği hedef RMS seviyesi."),
        ParamSpec("leveler_max_gain", "Leveler max gain", "float", 3.0, 1.0, 6.0, 0.5,
                  help="Leveler'ın uygulayabileceği maksimum kazanç — sessiz sesin ne kadar "
                       "yükseltileceğini sınırlar."),
        ParamSpec("leveler_floor", "Leveler floor RMS", "int", 200, 0, 2000, 10,
                  help="Altında sesin sessizlik sayılıp yükseltilmediği RMS; böylece kelimeler "
                       "arası gürültü büyütülmez."),
        ParamSpec("agc", "AGC (legacy)", "bool", False,
                  help="Eski otomatik kazanç kontrolü (AGC). Varsayılan kapalı; leveler tercih "
                       "edilir."),
        ParamSpec("agc_target", "AGC target RMS", "int", 3000, 500, 8000, 100,
                  help="Eski AGC için hedef RMS."),
        ParamSpec("agc_max_gain", "AGC max gain", "float", 8.0, 1.0, 20.0, 0.5,
                  help="Eski AGC'nin uygulayabileceği maksimum kazanç."),
        ParamSpec("nonspeech_atten", "Non-speech duck", "float", 1.0, 0.0, 1.0, 0.05,
                  help="Konuşma-dışı frame'lere uygulanan kazanç (1 = aynen tut, <1 kelimeler "
                       "arası arka planı kısar)."),
        ParamSpec("limiter_mode", "Limiter mode (0 soft/1 tanh)", "int", 0, 0, 1, 1,
                  help="Çıkış limiter şekli: 0 = soft-knee, 1 = tanh doygunluk."),
    ]

    _lib: ctypes.CDLL | None = None

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        if not LIB_PATH.exists():
            return False, f"libarfenhance.dylib not found — run `make build-c` (expected at {LIB_PATH})"
        try:
            cls._get_lib()
        except OSError as exc:
            return False, f"failed to load libarfenhance.dylib: {exc}"
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
        self._ae = lib.arf_audio_enhance_create()
        if not self._ae:
            raise MemoryError("arf_audio_enhance_create failed")
        lib.arf_audio_enhance_init(self._ae, sample_rate)
        self._apply_config()

    def _apply_config(self) -> None:
        c, ae, cfg = self._c, self._ae, self.config
        c.arf_audio_enhance_denoise_enable(ae, int(cfg["denoise"]))
        c.arf_audio_enhance_oversub_set(ae, cfg["oversub"])
        c.arf_audio_enhance_floor_set(ae, cfg["floor_gain"])
        c.arf_audio_enhance_hp_set(ae, float(cfg["hp_fc"]))
        c.arf_audio_enhance_hp_auto_set(ae, int(cfg["hp_auto"]), 5000.0, 0.30)
        c.arf_audio_enhance_shelf_set(ae, float(cfg["shelf_fc"]), cfg["shelf_gain"], cfg["shelf_q"])
        c.arf_audio_enhance_preemph_set(ae, cfg["preemph"])
        c.arf_audio_enhance_leveler_enable(ae, int(cfg["leveler"]))
        c.arf_audio_enhance_leveler_set(
            ae, float(cfg["leveler_target"]), cfg["leveler_max_gain"], float(cfg["leveler_floor"])
        )
        c.arf_audio_enhance_agc_enable(ae, int(cfg["agc"]))
        c.arf_audio_enhance_agc_target_set(ae, float(cfg["agc_target"]))
        c.arf_audio_enhance_agc_max_gain_set(ae, cfg["agc_max_gain"])
        c.arf_audio_enhance_nonspeech_atten_set(ae, cfg["nonspeech_atten"])
        c.arf_audio_enhance_limiter_mode_set(ae, int(cfg["limiter_mode"]))

    def process(self, frame: np.ndarray, is_speech: bool) -> np.ndarray:
        buf = np.ascontiguousarray(frame, dtype=np.int16).copy()
        ptr = buf.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        self._c.arf_audio_enhance_process(self._ae, ptr, len(buf), 1 if is_speech else 0)
        return buf

    @property
    def noise_rms(self) -> float:
        return float(self._c.arf_audio_enhance_noise_rms(self._ae))

    def reset(self) -> None:
        self._c.arf_audio_enhance_reset(self._ae)
        self._apply_config()  # reset restores defaults, so re-apply

    def close(self) -> None:
        if getattr(self, "_ae", None):
            self._c.arf_audio_enhance_destroy(self._ae)
            self._ae = None

    def __del__(self) -> None:
        self.close()
