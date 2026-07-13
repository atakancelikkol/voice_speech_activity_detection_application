"""Runtime engine selection: which engines are enabled and with what params.

Changes apply to the next call (UI toggles engines between calls with one
click; multiple engines run simultaneously by design).
"""

from __future__ import annotations

import logging
from typing import Any

from server.vad import registry
from server.vad.base import VadEngine

log = logging.getLogger("engines")


class EngineManager:
    def __init__(self) -> None:
        self.infos = registry.discover()
        self.enabled = {name: info.available for name, info in self.infos.items()}
        self.params: dict[str, dict[str, Any]] = {name: {} for name in self.infos}

    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for name, info in self.infos.items():
            entry = info.as_dict()
            entry["enabled"] = self.enabled[name] and info.available
            entry["values"] = {
                spec.name: self.params[name].get(spec.name, spec.default) for spec in info.params
            }
            out.append(entry)
        return out

    def configure(self, name: str, enabled: bool | None = None, params: dict[str, Any] | None = None) -> None:
        info = self.infos.get(name)
        if info is None:
            raise KeyError(name)
        if enabled is not None:
            if enabled and not info.available:
                raise ValueError(f"engine {name} unavailable: {info.reason}")
            self.enabled[name] = enabled
        if params is not None:
            by_name = {spec.name: spec for spec in info.params}
            unknown = set(params) - set(by_name)
            if unknown:
                raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
            merged = dict(self.params[name])
            merged.update({key: by_name[key].coerce(value) for key, value in params.items()})
            self.params[name] = merged

    def instantiate_enabled(self) -> dict[str, VadEngine]:
        engines: dict[str, VadEngine] = {}
        for name, info in self.infos.items():
            if not (self.enabled[name] and info.available):
                continue
            try:
                engines[name] = registry.create(info, self.params[name])
            except Exception as exc:  # a broken engine must not sink the whole recording
                log.warning("engine %s failed to start, skipping this call: %s", name, exc)
        return engines

    def instantiate(self, name: str) -> VadEngine:
        """One engine with its current params, regardless of enabled state
        (used to re-apply tuned params to an existing recording)."""
        info = self.infos.get(name)
        if info is None:
            raise KeyError(name)
        return registry.create(info, self.params[name])

    def config_of(self, name: str) -> dict[str, Any]:
        info = self.infos[name]
        return {spec.name: self.params[name].get(spec.name, spec.default) for spec in info.params}

    def active_configs(self) -> dict[str, dict[str, Any]]:
        return {
            name: self.config_of(name)
            for name in self.infos
            if self.enabled[name] and self.infos[name].available
        }
