import numpy as np

from server.rtp import g711
from server.rtp.packet import RtpPacket, seq_diff
from server.rtp.receiver import SAMPLES_PER_PACKET, RtpJitterBuffer


def make_packet(seq: int, value: int = 1000, ssrc: int = 0xABCD) -> RtpPacket:
    pcm = np.full(SAMPLES_PER_PACKET, value, dtype=np.int16)
    return RtpPacket(payload_type=0, sequence=seq & 0xFFFF, timestamp=seq * 160, ssrc=ssrc, payload=g711.encode(pcm))


def collect_buffer():
    chunks: list[np.ndarray] = []
    return chunks, RtpJitterBuffer(on_audio=chunks.append)


def test_packet_build_parse_round_trip():
    packet = RtpPacket(payload_type=0, sequence=42, timestamp=6720, ssrc=0xDEADBEEF, payload=b"\x55" * 160, marker=True)
    parsed = RtpPacket.parse(packet.build())
    assert parsed == packet


def test_seq_diff_wraparound():
    assert seq_diff(5, 0xFFFE) == 7
    assert seq_diff(0xFFFE, 5) == -7
    assert seq_diff(100, 100) == 0


def test_in_order_delivery():
    chunks, buffer = collect_buffer()
    for seq in range(10):
        buffer.push(make_packet(seq))
    assert len(chunks) == 10
    assert all(chunk[0] != 0 for chunk in chunks)
    assert buffer.stats.lost == 0


def test_single_reorder_is_healed():
    chunks, buffer = collect_buffer()
    buffer.push(make_packet(0, value=10))
    buffer.push(make_packet(2, value=30))  # early
    buffer.push(make_packet(1, value=20))  # late but within window
    values = [int(chunk[0]) for chunk in chunks]
    decoded = [int(g711.decode(g711.encode(np.array([v], dtype=np.int16)))[0]) for v in (10, 20, 30)]
    assert values == decoded
    assert buffer.stats.lost == 0


def test_loss_becomes_silence():
    chunks, buffer = collect_buffer()
    buffer.push(make_packet(0))
    for seq in range(4, 8):  # packets 1-3 lost, beyond the reorder window
        buffer.push(make_packet(seq))
    assert len(chunks) == 8
    silent = [i for i, chunk in enumerate(chunks) if not chunk.any()]
    assert silent == [1, 2, 3]
    assert buffer.stats.lost == 3


def test_sequence_wraparound_delivery():
    chunks, buffer = collect_buffer()
    for seq in (0xFFFE, 0xFFFF, 0, 1):
        buffer.push(make_packet(seq))
    assert len(chunks) == 4
    assert buffer.stats.lost == 0


def test_duplicate_and_late_dropped():
    chunks, buffer = collect_buffer()
    buffer.push(make_packet(0))
    buffer.push(make_packet(1))
    buffer.push(make_packet(0))  # duplicate
    assert len(chunks) == 2
    assert buffer.stats.late == 1


def test_foreign_ssrc_ignored():
    chunks, buffer = collect_buffer()
    buffer.push(make_packet(0))
    buffer.push(make_packet(1, ssrc=0x9999))
    assert len(chunks) == 1
    assert buffer.stats.bad == 1
