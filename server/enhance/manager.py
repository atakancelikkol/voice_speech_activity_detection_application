"""Runtime enhancer selection: which enhancer (if any) pre-processes the audio,
and with what params. At most one enhancer runs at a time (a chain of them
would just be confusing); enabling one applies to the next call."""

from __future__ import annotations

from typing import Any

from server.enhance import registry
from server.enhance.base import AudioEnhancer


class EnhancerManager:
    def __init__(self) -> None:
        self.infos = registry.discover()
        self.enabled = {name: False for name in self.infos}  # off by default
        self.params: dict[str, dict[str, Any]] = {name: {} for name in self.infos}

    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for name, info in self.infos.items():
            entry = info.as_dict()
            entry["enabled"] = self.enabled[name] and info.available
            entry["values"] = {spec.name: self.params[name].get(spec.name, spec.default) for spec in info.params}
            out.append(entry)
        return out

    def configure(self, name: str, enabled: bool | None = None, params: dict[str, Any] | None = None) -> None:
        info = self.infos.get(name)
        if info is None:
            raise KeyError(name)
        if enabled is not None:
            if enabled and not info.available:
                raise ValueError(f"enhancer {name} unavailable: {info.reason}")
            if enabled:
                for other in self.enabled:  # only one enhancer active at a time
                    self.enabled[other] = False
            self.enabled[name] = enabled
        if params is not None:
            by_name = {spec.name: spec for spec in info.params}
            unknown = set(params) - set(by_name)
            if unknown:
                raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
            merged = dict(self.params[name])
            merged.update({k: by_name[k].coerce(v) for k, v in params.items()})
            self.params[name] = merged

    def active_name(self) -> str | None:
        for name, on in self.enabled.items():
            if on and self.infos[name].available:
                return name
        return None

    def config_of(self, name: str) -> dict[str, Any]:
        info = self.infos[name]
        return {spec.name: self.params[name].get(spec.name, spec.default) for spec in info.params}

    def instantiate_active(self, sample_rate: int) -> AudioEnhancer | None:
        name = self.active_name()
        if name is None:
            return None
        return registry.create(self.infos[name], sample_rate, self.params[name])
