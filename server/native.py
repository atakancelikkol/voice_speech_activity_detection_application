"""Locate the bundled ctypes shared libraries with the OS-correct extension
(.dylib on macOS, .so on Linux) so the exact same engine code runs on a dev
Mac and inside a Linux container without per-call platform checks."""

from __future__ import annotations

import platform
from pathlib import Path

LIB_SUFFIX = ".dylib" if platform.system() == "Darwin" else ".so"
_THIRD_PARTY = Path(__file__).resolve().parent.parent / "third_party"


def lib_path(subdir: str, stem: str) -> Path:
    """Absolute path to third_party/<subdir>/<stem><.dylib|.so>."""
    return _THIRD_PARTY / subdir / f"{stem}{LIB_SUFFIX}"
