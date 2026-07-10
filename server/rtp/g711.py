"""G.711 mu-law codec (numpy). stdlib audioop was removed in Python 3.13,
so we carry our own tables; decode values match the classic g711.c tables."""

from __future__ import annotations

import numpy as np

_BIAS = 0x84
_CLIP = 32635


def _decode_byte(byte: int) -> int:
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -magnitude if sign else magnitude


DECODE_TABLE = np.array([_decode_byte(b) for b in range(256)], dtype=np.int16)


def decode(ulaw: bytes | np.ndarray) -> np.ndarray:
    """mu-law bytes -> int16 PCM."""
    data = np.frombuffer(ulaw, dtype=np.uint8) if isinstance(ulaw, (bytes, bytearray)) else ulaw
    return DECODE_TABLE[data]


def encode(pcm: np.ndarray) -> bytes:
    """int16 PCM -> mu-law bytes."""
    x = np.asarray(pcm, dtype=np.int32)
    sign = np.where(x < 0, 0x80, 0x00).astype(np.uint8)
    magnitude = np.minimum(np.abs(x), _CLIP) + _BIAS
    # exponent = index of the segment (highest set bit above bit 7);
    # frexp is exact on integers, unlike log2 with float rounding
    _, exp = np.frexp(magnitude.astype(np.float64))
    exponent = (exp - 8).astype(np.int32)
    mantissa = ((magnitude >> (exponent + 3)) & 0x0F).astype(np.uint8)
    return (~(sign | (exponent.astype(np.uint8) << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()
