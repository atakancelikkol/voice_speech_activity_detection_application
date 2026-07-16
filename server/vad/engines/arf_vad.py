"""arf_vad — the adaptive SNR detector from the arf-recog-adaptive-vad
UniMRCP plugin, via ctypes.

Two shared libraries, both mechanical extractions from the plugin tree:
- third_party/arf_vad/libarfvad.dylib: the plugin's arf_vad.c — adaptive
  noise floor (asymmetric one-pole tracker), SNR onset/offset gates with
  hysteresis and leaky integrators, DC blocker, zero-crossing fricative
  assist, proximity gates.
- third_party/libfvad/libfvad.dylib: the vendored WebRTC VAD the plugin
  fuses in as a spectral speech/noise vote (wind/click/room-noise gate).

The fusion here mirrors the plugin's recognizer engine exactly: libfvad
runs on every 10 ms frame, the speech fraction over the last fvad_window
frames is compared against a strict threshold before speech (open) and a
lenient one during speech (hold), and the resulting per-frame vote is fed
to arf_vad_spectral_vote_set before arf_vad_process. A noise vote vetoes
onset/offset; with use_fvad off the detector runs energy/SNR-only.
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

from server.native import lib_path
from server.vad.base import AudioFormat, EventKind, FrameScore, ParamSpec, VadEngine, VadEvent

LIB_PATH = lib_path("arf_vad", "libarfvad")
FVAD_LIB_PATH = lib_path("libfvad", "libfvad")

ARF_VAD_EVENT_NONE = 0
ARF_VAD_EVENT_ACTIVITY = 1
ARF_VAD_EVENT_INACTIVITY = 2
ARF_VAD_EVENT_NOINPUT = 3

# SNR mapped to score 1.0 for display. The decision variable ranges hugely: a
# quiet floor puts loud speech 40-75 dB over it, and transients (a close cough)
# reach ~80 dB. The old 30 dB ceiling saturated for most frames (curve pinned to
# the top, 30 read as the trigger). 80 dB spans the full observed range so even
# the loudest events stay on-scale and the curve keeps moving, while the
# onset/offset lines — low on this axis — mark where the on/off decision is made.
_SNR_FULL_SCALE_DB = 80.0


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(LIB_PATH))
    lib.arf_vad_create.restype = ctypes.c_void_p
    lib.arf_vad_create.argtypes = []
    for fn_name in ("arf_vad_destroy", "arf_vad_reset"):
        fn = getattr(lib, fn_name)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p]
    for setter in (
        "arf_vad_speech_timeout_set",
        "arf_vad_silence_timeout_set",
        "arf_vad_noinput_timeout_set",
        "arf_vad_frame_duration_set",
        "arf_vad_abs_silence_level_set",
        "arf_vad_onset_level_set",
        "arf_vad_onset_confirm_frames_set",
    ):
        fn = getattr(lib, setter)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    for setter in (
        "arf_vad_onset_snr_set",
        "arf_vad_offset_snr_set",
        "arf_vad_dominant_drop_set",
        "arf_vad_adaptive_proximity_set",
        "arf_vad_spec_bypass_snr_set",
    ):
        fn = getattr(lib, setter)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p, ctypes.c_double]
    for setter in ("arf_vad_zcr_enable", "arf_vad_spectral_vote_set"):
        fn = getattr(lib, setter)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.arf_vad_process.restype = ctypes.c_int
    lib.arf_vad_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]
    for getter in ("arf_vad_last_level_db", "arf_vad_noise_floor_db"):
        fn = getattr(lib, getter)
        fn.restype = ctypes.c_double
        fn.argtypes = [ctypes.c_void_p]
    return lib


def _load_fvad_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(FVAD_LIB_PATH))
    lib.fvad_new.restype = ctypes.c_void_p
    lib.fvad_new.argtypes = []
    lib.fvad_free.restype = None
    lib.fvad_free.argtypes = [ctypes.c_void_p]
    lib.fvad_reset.restype = None
    lib.fvad_reset.argtypes = [ctypes.c_void_p]
    lib.fvad_set_mode.restype = ctypes.c_int
    lib.fvad_set_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.fvad_set_sample_rate.restype = ctypes.c_int
    lib.fvad_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.fvad_process.restype = ctypes.c_int
    lib.fvad_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]
    return lib


class Engine(VadEngine):
    name = "arf_vad"
    display_name = "arf adaptive (SNR+fvad)"
    # Defaults are the recognizer engine's production values
    # (arf-recog-adaptive-vad/src/arf_recog_engine.c, the channel setup block),
    # not arf_vad.c's bare #define fallbacks — so the UI reflects how the plugin
    # actually runs on an 8 kHz telephony channel.
    params = [
        # adaptive SNR gates
        ParamSpec("onset_snr", "Onset SNR", "float", 18.0, 0.0, 30.0, 0.5, "dB",
                  help="Konuşmayı BAŞLATMAK için adaptif noise floor üzerinde gereken SNR. "
                       "Yüksek = daha katı başlangıç, sessiz sesleri yok sayar."),
        ParamSpec("offset_snr", "Offset SNR", "float", 5.0, 0.0, 30.0, 0.5, "dB",
                  help="Konuşmanın BİTTİĞİ kabul edildiği SNR. Onset SNR'ın altında tutulur "
                       "(hysteresis) ki kısa düşüşlerde konuşma kesilmesin."),
        ParamSpec("abs_silence_level", "Abs silence level", "int", 200, 0, 2000, 10,
                  help="SNR'dan bağımsız olarak bir frame'in her zaman sessizlik sayıldığı "
                       "mutlak genlik eşiği."),
        ParamSpec("use_zcr", "ZCR fricative assist", "bool", True,
                  help="Zero-crossing yardımı: düşük enerjili ama yüksek zero-crossing oranlı "
                       "ötümsüz frikatifleri (s, f, ş) yakalar."),
        # libfvad spectral gate (engine: mode 3, 25-frame window, 68/12 %)
        ParamSpec("use_fvad", "libfvad spectral gate", "bool", True,
                  help="WebRTC (libfvad) spektral konuşma/gürültü sınıflandırıcısını rüzgar, "
                       "tıklama ve sabit oda gürültüsüne karşı bir kapı olarak katar. "
                       "Kapalı = yalnızca enerji/SNR."),
        ParamSpec("fvad_mode", "fvad aggressiveness", "int", 3, 0, 3, 1,
                  help="libfvad agresifliği: 0 = hoşgörülü, 3 = konuşma-dışını reddetmede "
                       "en agresif."),
        ParamSpec("fvad_window", "fvad vote window", "int", 25, 1, 100, 1, "frames",
                  help="libfvad konuşma-oranı oylamasının ortalandığı son 10 ms'lik frame sayısı."),
        ParamSpec("fvad_open_pct", "fvad open threshold", "int", 68, 0, 100, 1, "%",
                  help="Konuşmayı AÇMAK (başlatmak) için pencerenin 'konuşma' oyu vermesi "
                       "gereken oran."),
        ParamSpec("fvad_hold_pct", "fvad hold threshold", "int", 12, 0, 100, 1, "%",
                  help="Konuşma başladıktan sonra onu TUTMAK için gereken daha düşük oran "
                       "(frikatif önleyici hysteresis)."),
        ParamSpec("spec_bypass_snr", "fvad veto bypass SNR", "float", 30.0, 0.0, 90.0, 0.5, "dB",
                  help="libfvad vetosunun yok sayıldığı SNR — çok yüksek ses, spektral oydan "
                       "bağımsız konuşma sayılır."),
        # proximity / dominant-talker gates (engine: onset_level 2500 + confirm 1
        # + adaptive proximity 6 dB on; dominant_drop off)
        ParamSpec("onset_level", "Onset level (abs)", "int", 2500, 0, 32767, 10,
                  help="Bir frame'in konuşma onset'i için uygun olması gereken mutlak genlik "
                       "(yakınlık / baskın konuşmacı kapısı)."),
        ParamSpec("onset_confirm_frames", "Onset confirm", "int", 1, 0, 50, 1, "frames",
                  help="Bir onset onaylanmadan önce gereken ardışık uygun frame sayısı."),
        ParamSpec("dominant_drop_db", "Dominant drop", "float", 0.0, 0.0, 60.0, 0.5, "dB",
                  help="Baskın konuşmacı segmentini bitiren, son tepe değerin altına düşüş "
                       "(0 = kapalı)."),
        ParamSpec("adaptive_margin_db", "Adaptive proximity", "float", 6.0, 0.0, 30.0, 0.5, "dB",
                  help="Konuşmayı kabul etmeden önce adaptif floor'un sinyalin altında tuttuğu "
                       "ek dB marjı (yakınlık kapısı)."),
        # timeouts (engine: speech 150 ms, silence 1300 ms; noinput comes from
        # the MRCP no-input-timeout header at runtime, 5 s is a sane default here)
        ParamSpec("speech_timeout", "Speech timeout", "int", 150, 0, 5000, 10, "ms",
                  help="Onset kapılarından sonra konuşma başlangıcının onaylanması için "
                       "minimum süre."),
        ParamSpec("silence_timeout", "Silence timeout", "int", 1300, 0, 5000, 10, "ms",
                  help="Konuşma bitti sayılmadan önce gereken sessizlik süresi."),
        ParamSpec("noinput_timeout", "No-input timeout", "int", 5000, 0, 60000, 100, "ms",
                  help="Başlangıçtan itibaren bu süre içinde konuşma olmazsa no-input olayı "
                       "tetiklenir."),
    ]

    _FORMAT = AudioFormat(sample_rate=8000, frame_samples=80)  # native 10 ms frames
    _lib: ctypes.CDLL | None = None
    _fvad_lib: ctypes.CDLL | None = None

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        for path in (LIB_PATH, FVAD_LIB_PATH):
            if not path.exists():
                return False, f"{path.name} not found — run `make build-c` (expected at {path})"
        try:
            cls._get_lib()
            cls._get_fvad_lib()
        except OSError as exc:
            return False, f"failed to load shared library: {exc}"
        return True, ""

    @classmethod
    def _get_lib(cls) -> ctypes.CDLL:
        if cls._lib is None:
            cls._lib = _load_lib()
        return cls._lib

    @classmethod
    def score_axis(cls, config: dict[str, Any]) -> dict[str, Any]:
        # The plotted value is SNR: how many dB the frame rises ABOVE the
        # adaptive noise floor (0 dB = the floor itself, the lane's baseline).
        # Scale ticks are plain dB gridlines; the two decision lines are the
        # onset SNR (signal must climb above it to START speech) and the offset
        # SNR (must fall below it to STOP), labelled in plain language so it is
        # clear the on/off decision lives at these lines, not at the top of the
        # axis. SNR is only the PRIMARY gate: the spectral (fvad) and proximity
        # gates can still veto, so the segment bars are the real outcome.
        full = _SNR_FULL_SCALE_DB
        ticks = [
            {"frac": db / full, "label": ("0 (taban)" if db == 0 else str(db)), "kind": "scale"}
            for db in (0, 20, 40, 60, 80)
        ]
        onset, offset = config["onset_snr"], config["offset_snr"]
        ticks.append({"frac": min(1.0, max(0.0, onset / full)), "label": f"başlar ≥{onset:g}", "kind": "threshold"})
        ticks.append({"frac": min(1.0, max(0.0, offset / full)), "label": f"biter <{offset:g}", "kind": "threshold"})
        return {"unit": "SNR dB", "ticks": ticks}

    @classmethod
    def _get_fvad_lib(cls) -> ctypes.CDLL:
        if cls._fvad_lib is None:
            cls._fvad_lib = _load_fvad_lib()
        return cls._fvad_lib

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        lib = self._get_lib()
        self._c = lib
        self._detector = lib.arf_vad_create()
        if not self._detector:
            raise MemoryError("arf_vad_create failed")
        lib.arf_vad_onset_snr_set(self._detector, self.config["onset_snr"])
        lib.arf_vad_offset_snr_set(self._detector, self.config["offset_snr"])
        lib.arf_vad_abs_silence_level_set(self._detector, self.config["abs_silence_level"])
        lib.arf_vad_zcr_enable(self._detector, int(self.config["use_zcr"]))
        lib.arf_vad_spec_bypass_snr_set(self._detector, self.config["spec_bypass_snr"])
        lib.arf_vad_onset_level_set(self._detector, self.config["onset_level"])
        lib.arf_vad_onset_confirm_frames_set(self._detector, self.config["onset_confirm_frames"])
        lib.arf_vad_dominant_drop_set(self._detector, self.config["dominant_drop_db"])
        lib.arf_vad_adaptive_proximity_set(self._detector, self.config["adaptive_margin_db"])
        lib.arf_vad_speech_timeout_set(self._detector, self.config["speech_timeout"])
        lib.arf_vad_silence_timeout_set(self._detector, self.config["silence_timeout"])
        lib.arf_vad_noinput_timeout_set(self._detector, self.config["noinput_timeout"])
        lib.arf_vad_frame_duration_set(self._detector, int(self._FORMAT.frame_ms))

        self._fvad = None
        if self.config["use_fvad"]:
            fv = self._get_fvad_lib()
            self._fv = fv
            self._fvad = fv.fvad_new()
            if not self._fvad:
                raise MemoryError("fvad_new failed")
            self._configure_fvad()
        self._reset_runtime()

    def _configure_fvad(self) -> None:
        # fvad_reset restores mode/rate to defaults, so (re)set them after
        if self._fv.fvad_set_mode(self._fvad, self.config["fvad_mode"]) != 0:
            raise ValueError(f"invalid fvad mode {self.config['fvad_mode']}")
        if self._fv.fvad_set_sample_rate(self._fvad, self._FORMAT.sample_rate) != 0:
            raise ValueError(f"invalid fvad sample rate {self._FORMAT.sample_rate}")

    def _reset_runtime(self) -> None:
        self._noinput_reported = False
        self._in_speech = False
        win = self.config["fvad_window"]
        self._fvad_ring = [0] * win
        self._fvad_pos = 0
        self._fvad_sum = 0
        self._fvad_fill = 0

    @property
    def input_format(self) -> AudioFormat:
        return self._FORMAT

    def process(self, frame: np.ndarray, frame_start_ms: float) -> FrameScore:
        frame = np.ascontiguousarray(frame, dtype=np.int16)
        ptr = frame.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        if self._fvad is not None:
            self._c.arf_vad_spectral_vote_set(self._detector, self._spectral_vote(ptr, len(frame)))
        code = self._c.arf_vad_process(self._detector, ptr, len(frame))
        # the decision variable is SNR over the adaptive floor — plot that
        snr = self._c.arf_vad_last_level_db(self._detector) - self._c.arf_vad_noise_floor_db(self._detector)
        event = self._map_event(code, frame_start_ms + self._FORMAT.frame_ms)
        score = min(1.0, max(0.0, snr / _SNR_FULL_SCALE_DB))
        return FrameScore(score=score, raw=snr, event=event)

    def _spectral_vote(self, ptr, count: int) -> int:
        """Sliding speech-fraction window over per-frame libfvad decisions,
        exactly as the plugin's recognizer engine feeds arf_vad: strict
        threshold to open (start speech), lenient to hold (anti-fricative)."""
        d = self._fv.fvad_process(self._fvad, ptr, count)
        if d < 0:  # invalid frame length: no hint, energy-only frame
            return -1
        self._fvad_sum -= self._fvad_ring[self._fvad_pos]
        self._fvad_ring[self._fvad_pos] = d
        self._fvad_sum += d
        self._fvad_pos = (self._fvad_pos + 1) % len(self._fvad_ring)
        if self._fvad_fill < len(self._fvad_ring):
            self._fvad_fill += 1
        pct = self.config["fvad_hold_pct"] if self._in_speech else self.config["fvad_open_pct"]
        return 1 if self._fvad_sum * 100 >= pct * self._fvad_fill else 0

    def _map_event(self, code: int, frame_end_ms: float) -> VadEvent | None:
        # Transitions confirm only after the leaky integrator has accrued the
        # timeout, so onsets/offsets are backdated by (at least) that much.
        if code == ARF_VAD_EVENT_ACTIVITY:
            self._noinput_reported = False
            self._in_speech = True
            return VadEvent(EventKind.SPEECH_START, max(0.0, frame_end_ms - self.config["speech_timeout"]))
        if code == ARF_VAD_EVENT_INACTIVITY:
            self._in_speech = False
            return VadEvent(EventKind.SPEECH_END, max(0.0, frame_end_ms - self.config["silence_timeout"]))
        if code == ARF_VAD_EVENT_NOINPUT:
            # the C detector re-reports NOINPUT once per timeout window — report once
            if self._noinput_reported:
                return None
            self._noinput_reported = True
            return VadEvent(EventKind.NOINPUT, frame_end_ms)
        return None

    def reset(self) -> None:
        self._c.arf_vad_reset(self._detector)
        if self._fvad is not None:
            self._fv.fvad_reset(self._fvad)
            self._configure_fvad()
        self._reset_runtime()

    def close(self) -> None:
        if getattr(self, "_detector", None):
            self._c.arf_vad_destroy(self._detector)
            self._detector = None
        if getattr(self, "_fvad", None):
            self._fv.fvad_free(self._fvad)
            self._fvad = None

    def __del__(self) -> None:
        self.close()
