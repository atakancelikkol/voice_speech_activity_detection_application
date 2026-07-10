"""Engine discovery. Adding an engine = one module + one entry here."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from server.vad.base import ParamSpec, VadEngine

ENGINE_MODULES = [
    "server.vad.engines.unimrcp_vad",
    "server.vad.engines.silero_vad",
    "server.vad.engines.ten_vad",
    "server.vad.engines.arf_vad",
]


@dataclass
class EngineInfo:
    name: str
    display_name: str
    available: bool
    reason: str
    params: list[ParamSpec]
    cls: type[VadEngine] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "available": self.available,
            "reason": self.reason,
            "params": [vars(p) for p in self.params],
        }


def discover() -> dict[str, EngineInfo]:
    infos: dict[str, EngineInfo] = {}
    for module_name in ENGINE_MODULES:
        fallback_name = module_name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(module_name)
            cls: type[VadEngine] = module.Engine
        except Exception as exc:  # missing optional dep must not take the app down
            infos[fallback_name] = EngineInfo(fallback_name, fallback_name, False, f"import failed: {exc}", [], None)
            continue
        available, reason = cls.probe()
        infos[cls.name] = EngineInfo(cls.name, cls.display_name, available, reason, list(cls.params), cls)
    return infos


def create(info: EngineInfo, params: dict[str, Any] | None = None) -> VadEngine:
    if info.cls is None or not info.available:
        raise RuntimeError(f"engine {info.name} unavailable: {info.reason}")
    return info.cls(params)
