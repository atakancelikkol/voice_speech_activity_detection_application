import numpy as np

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
