"""Enhancer discovery. Adding an enhancer = one module + one entry here."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from server.enhance.base import AudioEnhancer
from server.vad.base import ParamSpec

ENHANCER_MODULES = [
    "server.enhance.engines.arf_enhance",
    "server.enhance.engines.deepfilter",
]


@dataclass
class EnhancerInfo:
    name: str
    display_name: str
    available: bool
    reason: str
    params: list[ParamSpec]
    cls: type[AudioEnhancer] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "available": self.available,
            "reason": self.reason,
            "params": [vars(p) for p in self.params],
        }


def discover() -> dict[str, EnhancerInfo]:
    infos: dict[str, EnhancerInfo] = {}
    for module_name in ENHANCER_MODULES:
        fallback = module_name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(module_name)
            cls: type[AudioEnhancer] = module.Engine
        except Exception as exc:
            infos[fallback] = EnhancerInfo(fallback, fallback, False, f"import failed: {exc}", [], None)
            continue
        available, reason = cls.probe()
        infos[cls.name] = EnhancerInfo(cls.name, cls.display_name, available, reason, list(cls.params), cls)
    return infos


def create(info: EnhancerInfo, sample_rate: int, params: dict[str, Any] | None = None) -> AudioEnhancer:
    if info.cls is None or not info.available:
        raise RuntimeError(f"enhancer {info.name} unavailable: {info.reason}")
    return info.cls(sample_rate, params)
