"""ten_vad_df — TEN VAD fed through the df_enhance noise suppressor.

Mirrors the production unimrcp arf-recog-ten-vad path: the enhancer runs at
the 8 kHz telephony rate on 10 ms frames (in place, before anything else),
the enhanced audio is upsampled to 16 kHz and TEN VAD scores it in
256-sample hops. Comparing this engine's lane against plain `ten_vad` in the
same session is the A/B for "does pre-VAD denoising help TEN VAD".

The is_speech hint fed back to the enhancer is the hysteresis in-speech
state, exactly like the plugin feeds its `in_speech` flag.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import soxr

from server.vad.base import AudioFormat, FrameScore, ParamSpec, VadEngine
from server.vad.segments import ProbabilityHysteresis

SOURCE_RATE = 8000
FRAME = 80  # 10 ms at 8 kHz, matching the production media frame
VAD_RATE = 16000
HOP = 256  # 16 ms at 16 kHz


class Engine(VadEngine):
    name = "ten_vad_df"
    display_name = "TEN VAD + DF enhance"
    params = [
        ParamSpec("threshold", "Speech threshold", "float", 0.5, 0.05, 0.95, 0.05,
                  help="Bir frame'in konuşma sayıldığı nöral konuşma-olasılığı eşiği. "
                       "Düşük = daha hassas (daha çok konuşma, daha çok yanlış pozitif)."),
        ParamSpec("min_speech_ms", "Min speech", "int", 250, 0, 2000, 10, "ms",
                  help="Bundan kısa konuşma segmentleri sahte kabul edilip atılır."),
        ParamSpec("min_silence_ms", "Min silence", "int", 300, 0, 5000, 10, "ms",
                  help="Bundan kısa sessizlik bir segmenti bitirmez; kelimeler arası kısa "
                       "duraklar köprülenir."),
        ParamSpec("speech_pad_ms", "Speech padding", "int", 30, 0, 500, 10, "ms",
                  help="Algılanan segmentin her iki ucuna eklenen dolgu, böylece "
                       "başlangıç/bitiş kırpılmaz."),
        ParamSpec("enhance", "Enhance (df_enhance)", "bool", True,
                  help="df_enhance ön-işlemesi. Kapalıyken bu lane, 8k->16k yolunu birebir "
                       "izleyen ham bir ten_vad kopyasına döner (kontrol grubu)."),
        ParamSpec("stage2", "Deep filtering (stage 2)", "bool", True,
                  help="Multi-frame deep filtering aşaması; kapalıyken sadece ERB-band "
                       "Wiener (stage 1)."),
        ParamSpec("gain_floor_db", "Gain floor", "float", -15.0, -30.0, 0.0, 1.0, "dB",
                  help="Enhancer stage-1 kazanç tabanı. Daha negatif = daha agresif "
                       "bastırma."),
    ]

    _FORMAT = AudioFormat(sample_rate=SOURCE_RATE, frame_samples=FRAME)

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        try:
            from ten_vad import TenVad

            TenVad(HOP, 0.5)
        except Exception as exc:
            return False, f"ten-vad unavailable: {exc}"
        from server.enhance.engines.deepfilter import Engine as DfEnhancer

        ok, reason = DfEnhancer.probe()
        if not ok:
            return False, reason
        return True, ""

    @classmethod
    def score_axis(cls, config: dict[str, Any]) -> dict[str, Any]:
        thr = config["threshold"]
        return {
            "unit": "prob",
            "ticks": [
                {"frac": 0.0, "label": "0", "kind": "scale"},
                {"frac": 0.5, "label": "0.5", "kind": "scale"},
                {"frac": 1.0, "label": "1", "kind": "scale"},
                {"frac": thr, "label": f"{thr:.2f}", "kind": "threshold"},
            ],
        }

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        from ten_vad import TenVad

        self._vad = TenVad(HOP, self.config["threshold"])
        self._hysteresis = ProbabilityHysteresis(
            threshold=self.config["threshold"],
            min_speech_ms=self.config["min_speech_ms"],
            min_silence_ms=self.config["min_silence_ms"],
            speech_pad_ms=self.config["speech_pad_ms"],
        )
        self._enhancer = None
        if self.config["enhance"]:
            from server.enhance.engines.deepfilter import Engine as DfEnhancer

            self._enhancer = DfEnhancer(SOURCE_RATE, {
                "stage2": self.config["stage2"],
                "gain_floor_db": self.config["gain_floor_db"],
            })
        self._make_stream()

    def _make_stream(self) -> None:
        self._resampler = soxr.ResampleStream(SOURCE_RATE, VAD_RATE, 1, dtype="int16")
        self._hop_buf = np.empty(0, dtype=np.int16)
        self._prob = 0.0
        self._in_speech = False

    @property
    def input_format(self) -> AudioFormat:
        return self._FORMAT

    def process(self, frame: np.ndarray, frame_start_ms: float) -> FrameScore:
        pcm = np.ascontiguousarray(frame, dtype=np.int16)
        if self._enhancer is not None:
            # production feedback loop: the plugin passes its in_speech flag
            pcm = self._enhancer.process(pcm, self._in_speech)
        up = self._resampler.resample_chunk(pcm)
        if len(up):
            self._hop_buf = np.concatenate([self._hop_buf, up.astype(np.int16)])
        while len(self._hop_buf) >= HOP:
            hop, self._hop_buf = self._hop_buf[:HOP], self._hop_buf[HOP:]
            prob, _flag = self._vad.process(np.ascontiguousarray(hop))
            self._prob = float(prob)
        event = self._hysteresis.update(self._prob, frame_start_ms,
                                        self._FORMAT.frame_ms)
        if event is not None:
            from server.vad.base import EventKind

            self._in_speech = event.kind is EventKind.SPEECH_START
        return FrameScore(score=self._prob, raw=self._prob, event=event)

    def reset(self) -> None:
        from ten_vad import TenVad

        self._vad = TenVad(HOP, self.config["threshold"])
        self._hysteresis.reset()
        if self._enhancer is not None:
            self._enhancer.reset()
        self._make_stream()

    def close(self) -> None:
        if self._enhancer is not None:
            self._enhancer.close()
            self._enhancer = None
