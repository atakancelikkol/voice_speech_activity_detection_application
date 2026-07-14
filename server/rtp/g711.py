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


# --- A-law (G.711 PCMA) ---------------------------------------------------
# Turkey/Europe telephony uses A-law; the mu-law above is North America/Japan.
# Tables match CCITT G.711 / the classic g711.c alaw routines.
_ALAW_MASK = 0x55
_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _decode_alaw_byte(byte: int) -> int:
    byte ^= _ALAW_MASK
    t = (byte & 0x0F) << 4
    seg = (byte & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if byte & 0x80 else -t


ALAW_DECODE_TABLE = np.array([_decode_alaw_byte(b) for b in range(256)], dtype=np.int16)


def decode_alaw(alaw: bytes | np.ndarray) -> np.ndarray:
    """A-law (PCMA) bytes -> int16 PCM."""
    data = np.frombuffer(alaw, dtype=np.uint8) if isinstance(alaw, (bytes, bytearray)) else alaw
    return ALAW_DECODE_TABLE[data]


def _encode_alaw_val(pcm_val: int) -> int:
    pcm_val >>= 3
    if pcm_val >= 0:
        mask = 0xD5  # sign bit set for positive samples
    else:
        mask = 0x55
        pcm_val = -pcm_val - 1
    seg = next((i for i, end in enumerate(_SEG_AEND) if pcm_val <= end), 8)
    if seg >= 8:  # out of range -> clamp to max magnitude
        return 0x7F ^ mask
    aval = seg << 4
    aval |= (pcm_val >> 1) & 0x0F if seg < 2 else (pcm_val >> seg) & 0x0F
    return aval ^ mask


# indexed by the sample's uint16 bit pattern (int16 & 0xFFFF)
ALAW_ENCODE_TABLE = np.array(
    [_encode_alaw_val(i - 65536 if i >= 32768 else i) for i in range(65536)], dtype=np.uint8
)


def encode_alaw(pcm: np.ndarray) -> bytes:
    """int16 PCM -> A-law (PCMA) bytes."""
    return ALAW_ENCODE_TABLE[np.asarray(pcm, dtype=np.int16).astype(np.uint16)].tobytes()


def imprint(pcm: np.ndarray, law: str) -> np.ndarray:
    """Round-trip PCM through a G.711 codec so a clean signal carries the exact
    companding quantization the production VAD sees on the wire. `law` is
    'alaw'/'pcma' (Turkey/Europe) or 'ulaw'/'pcmu'."""
    key = law.strip().lower()
    if key in ("alaw", "pcma", "a"):
        return decode_alaw(encode_alaw(pcm))
    if key in ("ulaw", "mulaw", "pcmu", "u"):
        return decode(encode(pcm))
    raise ValueError(f"unknown G.711 law: {law!r}")
