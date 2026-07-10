"""Core VAD engine plugin interface.

Every engine declares the audio format it wants (sample rate + samples per
call) and turns frames into a per-frame score plus optional speech events.
All millisecond values live on the session timeline defined by the decoded
8 kHz stream; events may carry timestamps earlier than the current frame
(backdating), because detectors only confirm speech onset after their
speech-timeout has elapsed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Literal

import numpy as np


@dataclass(frozen=True)
class AudioFormat:
    """Input format an engine expects for each process() call."""

    sample_rate: int
    frame_samples: int

    @property
    def frame_ms(self) -> float:
        return self.frame_samples * 1000.0 / self.sample_rate


class EventKind(str, Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    NOINPUT = "noinput"


@dataclass
class VadEvent:
    kind: EventKind
    at_ms: float


@dataclass
class FrameScore:
    score: float  # normalized 0..1 for display
    raw: float  # engine-native value (energy level, probability, ...)
    event: VadEvent | None = None


@dataclass(frozen=True)
class ParamSpec:
    """Tunable engine parameter; drives the UI parameter panel."""

    name: str
    label: str
    type: Literal["int", "float", "bool"]
    default: Any
    min: Any = None
    max: Any = None
    step: Any = None
    unit: str = ""

    def coerce(self, value: Any) -> Any:
        if self.type == "bool":
            # CLI --param values arrive as strings; bool("0") would be True
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        value = int(value) if self.type == "int" else float(value)
        if self.min is not None:
            value = max(self.min, value)
        if self.max is not None:
            value = min(self.max, value)
        return value


def resolve_params(specs: list[ParamSpec], given: dict[str, Any]) -> dict[str, Any]:
    """Merge user-supplied values over defaults; unknown keys are rejected."""
    by_name = {spec.name: spec for spec in specs}
    unknown = set(given) - set(by_name)
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    resolved = {spec.name: spec.default for spec in specs}
    for name, value in given.items():
        resolved[name] = by_name[name].coerce(value)
    return resolved


class VadEngine(ABC):
    """Base class for VAD engine plugins.

    Subclasses set the class attributes, implement input_format/process,
    and may raise from __init__ if their backing resources are missing
    (the registry then marks them unavailable).
    """

    name: ClassVar[str]
    display_name: ClassVar[str]
    params: ClassVar[list[ParamSpec]] = []

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        """Cheap availability check: (available, reason-if-not)."""
        return True, ""

    def __init__(self, params: dict[str, Any] | None = None):
        self.config = resolve_params(self.params, params or {})

    @property
    @abstractmethod
    def input_format(self) -> AudioFormat: ...

    @abstractmethod
    def process(self, frame: np.ndarray, frame_start_ms: float) -> FrameScore:
        """Process one int16 frame of exactly input_format.frame_samples samples."""

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass
