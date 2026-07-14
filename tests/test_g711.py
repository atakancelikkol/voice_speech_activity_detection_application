import numpy as np
import pytest

from server.rtp import g711


def test_known_table_values():
    # canonical g711.c ulaw2linear values
    assert g711.DECODE_TABLE[0x00] == -32124
    assert g711.DECODE_TABLE[0x80] == 32124
    assert g711.DECODE_TABLE[0xFF] == 0
    assert g711.DECODE_TABLE[0x7F] == 0


def test_encode_zero_and_extremes():
    assert g711.encode(np.array([0], dtype=np.int16)) == b"\xff"
    assert g711.encode(np.array([32767], dtype=np.int16)) == b"\x80"
    assert g711.encode(np.array([-32768], dtype=np.int16)) == b"\x00"


def test_round_trip_error_bounded():
    x = np.arange(-32768, 32768, dtype=np.int16)
    decoded = g711.decode(g711.encode(x)).astype(np.int32)
    error = np.abs(decoded - np.clip(x.astype(np.int32), -32635, 32635))
    # max quantization step in the top mu-law segment is 1024
    assert error.max() <= 512
    # small amplitudes quantize finely (abs in int32: abs(int16 -32768) overflows)
    small = np.abs(x.astype(np.int32)) < 100
    assert error[small].max() <= 4


def test_decode_monotonic_per_sign():
    # bytes 0x80..0xFF decode to positive values, descending magnitude
    positive = g711.DECODE_TABLE[0x80:0x100].astype(np.int32)
    assert (np.diff(positive) <= 0).all()
    negative = g711.DECODE_TABLE[0x00:0x80].astype(np.int32)
    assert (np.diff(negative) >= 0).all()


def test_encode_decode_idempotent_on_table():
    # decoding any byte and re-encoding must return the same byte
    all_bytes = bytes(range(256))
    re_encoded = g711.encode(g711.decode(all_bytes))
    # +0/-0 alias (0x7F/0xFF) is the only permitted difference
    for original, back in zip(all_bytes, re_encoded):
        if g711.DECODE_TABLE[original] == 0:
            assert back in (0x7F, 0xFF)
        else:
            assert back == original


# --- A-law (PCMA) — Turkey/Europe telephony -------------------------------


def test_alaw_known_table_values():
    # canonical g711.c alaw2linear values (verified against audioop.alaw2lin)
    assert g711.ALAW_DECODE_TABLE[0xD5] == 8  # smallest positive step (no true 0)
    assert g711.ALAW_DECODE_TABLE[0x55] == -8
    assert g711.ALAW_DECODE_TABLE[0xAA] == 32256  # max positive magnitude
    assert g711.ALAW_DECODE_TABLE[0x2A] == -32256


def test_alaw_encode_zero_and_extremes():
    assert g711.encode_alaw(np.array([0], dtype=np.int16)) == b"\xd5"
    assert g711.encode_alaw(np.array([32767], dtype=np.int16)) == b"\xaa"
    assert g711.encode_alaw(np.array([-32768], dtype=np.int16)) == b"\x2a"


def test_alaw_round_trip_error_bounded():
    x = np.arange(-32768, 32768, dtype=np.int16)
    decoded = g711.decode_alaw(g711.encode_alaw(x)).astype(np.int32)
    error = np.abs(decoded - x.astype(np.int32))
    assert error.max() <= 512  # top A-law segment step
    small = np.abs(x.astype(np.int32)) < 100
    assert error[small].max() <= 8  # fine near zero


def test_alaw_encode_decode_idempotent_on_table():
    # A-law has no true zero, so every byte round-trips exactly (no ±0 alias)
    all_bytes = bytes(range(256))
    assert g711.encode_alaw(g711.decode_alaw(all_bytes)) == all_bytes


def test_imprint_dispatch_and_reduces_amplitude_resolution():
    x = np.arange(-32768, 32768, dtype=np.int16)
    assert np.array_equal(g711.imprint(x, "alaw"), g711.decode_alaw(g711.encode_alaw(x)))
    assert np.array_equal(g711.imprint(x, "PCMU"), g711.decode(g711.encode(x)))
    with pytest.raises(ValueError):
        g711.imprint(x, "opus")
