"""unimrcp_vad — the actual UniMRCP activity detector C code, via ctypes.

The shared library (third_party/unimrcp_vad/libuvad.dylib) is a mechanical
extraction of mpf_activity_detector.c; behaviour is identical to UniMRCP.
"""

from __future__ import annotations

import ctypes
import math
from typing import Any

import numpy as np

from server.native import lib_path
from server.vad.base import AudioFormat, EventKind, FrameScore, ParamSpec, VadEngine, VadEvent

LIB_PATH = lib_path("unimrcp_vad", "libuvad")

UVAD_EVENT_NONE = 0
UVAD_EVENT_ACTIVITY = 1
UVAD_EVENT_INACTIVITY = 2
UVAD_EVENT_NOINPUT = 3

_LOG_FULL_SCALE = math.log1p(32767.0)


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(LIB_PATH))
    lib.uvad_create.restype = ctypes.c_void_p
    lib.uvad_create.argtypes = []
    lib.uvad_destroy.restype = None
    lib.uvad_destroy.argtypes = [ctypes.c_void_p]
    lib.uvad_reset.restype = None
    lib.uvad_reset.argtypes = [ctypes.c_void_p]
    for setter in (
        "uvad_level_threshold_set",
        "uvad_noinput_timeout_set",
        "uvad_speech_timeout_set",
        "uvad_silence_timeout_set",
        "uvad_frame_duration_set",
    ):
        fn = getattr(lib, setter)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.uvad_process.restype = ctypes.c_int
    lib.uvad_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]
    lib.uvad_level.restype = ctypes.c_size_t
    lib.uvad_level.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]
    return lib


class Engine(VadEngine):
    name = "unimrcp_vad"
    display_name = "unimrcp (energy)"
    # Defaults are the production values the recognizer engine configures
    # (arf-recog-kursat/src/arf_recog_engine.c channel setup) rather than
    # mpf_activity_detector's bare defaults (level_threshold=2). The threshold
    # is a frame's mean |sample| of 16-bit linear PCM; it ranges well above the
    # "0..255" the source comment implies, so the range stays open for tuning.
    # noinput_timeout comes from the MRCP no-input-timeout header at runtime;
    # 5 s is a sane default here.
    params = [
        ParamSpec("level_threshold", "Level threshold", "int", 140, 0, 8000, 1,
                  help="Bir frame'in ortalama |örnek| genliği bunu aştığında konuşma işaretlenir. "
                       "Yüksek = daha yüksek ses gerekir, arka plan gürültüsünde daha az yanlış tetikleme."),
        ParamSpec("speech_timeout", "Speech timeout", "int", 350, 0, 5000, 10, "ms",
                  help="Konuşma başlangıcı onaylanmadan önce seviyenin threshold üzerinde kalması "
                       "gereken süre. Kısa sıçramaları filtreler."),
        ParamSpec("silence_timeout", "Silence timeout", "int", 1100, 0, 5000, 10, "ms",
                  help="Konuşma bitti sayılmadan önce seviyenin threshold altında kalması "
                       "gereken süre."),
        ParamSpec("noinput_timeout", "No-input timeout", "int", 5000, 0, 60000, 100, "ms",
                  help="Başlangıçtan itibaren bu süre içinde konuşma algılanmazsa no-input olayı "
                       "tetiklenir (production'da IVR beklemeyi bırakır)."),
    ]

    _FORMAT = AudioFormat(sample_rate=8000, frame_samples=80)  # native 10 ms frames
    _lib: ctypes.CDLL | None = None

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        if not LIB_PATH.exists():
            return False, f"libuvad.dylib not found — run `make build-c` (expected at {LIB_PATH})"
        try:
            cls._get_lib()
        except OSError as exc:
            return False, f"failed to load libuvad.dylib: {exc}"
        return True, ""

    @classmethod
    def _get_lib(cls) -> ctypes.CDLL:
        if cls._lib is None:
            cls._lib = _load_lib()
        return cls._lib

    @classmethod
    def score_axis(cls, config: dict[str, Any]) -> dict[str, Any]:
        # score = log1p(mean|sample|) / log1p(32767): a log-compressed amplitude,
        # not a probability. Present the scale as dBFS (0 dB = full scale) so the
        # energy reads in familiar dB, and mark the level_threshold decision line
        # in its native mean-|sample| units (the same units the param panel tunes).
        def frac_of_level(level: float) -> float:
            return min(1.0, max(0.0, math.log1p(max(0.0, level)) / _LOG_FULL_SCALE))

        def frac_of_dbfs(dbfs: float) -> float:
            return frac_of_level(32767.0 * 10.0 ** (dbfs / 20.0))

        ticks = [{"frac": frac_of_dbfs(db), "label": str(db), "kind": "scale"} for db in (-60, -40, -20, 0)]
        thr = config["level_threshold"]
        ticks.append({"frac": frac_of_level(thr), "label": f"{thr:g}", "kind": "threshold"})
        return {"unit": "dBFS", "ticks": ticks}

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        lib = self._get_lib()
        self._c = lib
        self._detector = lib.uvad_create()
        if not self._detector:
            raise MemoryError("uvad_create failed")
        lib.uvad_level_threshold_set(self._detector, self.config["level_threshold"])
        lib.uvad_speech_timeout_set(self._detector, self.config["speech_timeout"])
        lib.uvad_silence_timeout_set(self._detector, self.config["silence_timeout"])
        lib.uvad_noinput_timeout_set(self._detector, self.config["noinput_timeout"])
        lib.uvad_frame_duration_set(self._detector, int(self._FORMAT.frame_ms))
        self._noinput_reported = False

    @property
    def input_format(self) -> AudioFormat:
        return self._FORMAT

    def process(self, frame: np.ndarray, frame_start_ms: float) -> FrameScore:
        frame = np.ascontiguousarray(frame, dtype=np.int16)
        ptr = frame.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        raw = float(self._c.uvad_level(ptr, len(frame)))
        code = self._c.uvad_process(self._detector, ptr, len(frame))
        event = self._map_event(code, frame_start_ms + self._FORMAT.frame_ms)
        score = math.log1p(raw) / _LOG_FULL_SCALE
        return FrameScore(score=min(1.0, score), raw=raw, event=event)

    def _map_event(self, code: int, frame_end_ms: float) -> VadEvent | None:
        # The C detector confirms transitions only after its timeout has
        # elapsed, so onsets/offsets are backdated to the transition start.
        if code == UVAD_EVENT_ACTIVITY:
            self._noinput_reported = False
            return VadEvent(EventKind.SPEECH_START, max(0.0, frame_end_ms - self.config["speech_timeout"]))
        if code == UVAD_EVENT_INACTIVITY:
            return VadEvent(EventKind.SPEECH_END, max(0.0, frame_end_ms - self.config["silence_timeout"]))
        if code == UVAD_EVENT_NOINPUT:
            # the C detector re-reports NOINPUT every frame past the timeout
            # (unimrcp callers end the call on the first one) — report once
            if self._noinput_reported:
                return None
            self._noinput_reported = True
            return VadEvent(EventKind.NOINPUT, frame_end_ms)
        return None

    def reset(self) -> None:
        self._c.uvad_reset(self._detector)
        self._noinput_reported = False

    def close(self) -> None:
        if getattr(self, "_detector", None):
            self._c.uvad_destroy(self._detector)
            self._detector = None

    def __del__(self) -> None:
        self.close()
