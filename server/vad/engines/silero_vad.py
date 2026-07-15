"""silero_vad — Silero VAD v5 ONNX model via onnxruntime.

Model signature (verified): input [batch, 64 context + 512 samples] float32
normalized to [-1, 1], state [2, batch, 128], sr scalar int64; outputs
speech probability [batch, 1] and the next state. Segmentation happens in
the shared ProbabilityHysteresis on top of the per-chunk probability.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from server.vad.base import AudioFormat, FrameScore, ParamSpec, VadEngine
from server.vad.segments import ProbabilityHysteresis

REPO_ROOT = Path(__file__).resolve().parents[3]
MODEL_PATH = Path(os.environ.get("VAD_SILERO_MODEL", REPO_ROOT / "models" / "silero_vad.onnx"))

SAMPLE_RATE = 16000
CHUNK = 512  # 32 ms
CONTEXT = 64  # v5 prepends 64 samples of context at 16 kHz


class Engine(VadEngine):
    name = "silero_vad"
    display_name = "Silero VAD (ONNX)"
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
    ]

    _FORMAT = AudioFormat(sample_rate=SAMPLE_RATE, frame_samples=CHUNK)

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        try:
            import onnxruntime  # noqa: F401
        except ImportError as exc:
            return False, f"onnxruntime not installed: {exc}"
        if not MODEL_PATH.exists():
            return False, f"model not found — run `make models` (expected at {MODEL_PATH})"
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
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(MODEL_PATH), options, providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._hysteresis = ProbabilityHysteresis(
            threshold=self.config["threshold"],
            min_speech_ms=self.config["min_speech_ms"],
            min_silence_ms=self.config["min_silence_ms"],
            speech_pad_ms=self.config["speech_pad_ms"],
        )
        self.reset()

    @property
    def input_format(self) -> AudioFormat:
        return self._FORMAT

    def process(self, frame: np.ndarray, frame_start_ms: float) -> FrameScore:
        x = frame.astype(np.float32) / 32768.0
        x = np.concatenate([self._context, x])[np.newaxis, :]
        prob_out, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        self._context = x[0, -CONTEXT:]
        prob = float(prob_out[0, 0])
        event = self._hysteresis.update(prob, frame_start_ms, self._FORMAT.frame_ms)
        return FrameScore(score=prob, raw=prob, event=event)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._hysteresis.reset()
