"""RTP receive path: datagram endpoint + small reorder buffer.

Losses are replaced with silence so the session timeline stays sample-exact;
late/duplicate packets are dropped.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from server.rtp import g711
from server.rtp.packet import RtpPacket, seq_diff

SAMPLES_PER_PACKET = 160  # PCMU, 20 ms @ 8 kHz


@dataclass
class RtpStats:
    received: int = 0
    lost: int = 0
    late: int = 0
    bad: int = 0


@dataclass
class RtpJitterBuffer:
    """Reorders slightly-late packets; anything further out is silence."""

    on_audio: Callable[[np.ndarray], None]
    samples_per_packet: int = SAMPLES_PER_PACKET
    max_reorder: int = 2
    stats: RtpStats = field(default_factory=RtpStats)

    def __post_init__(self) -> None:
        self._expected: int | None = None
        self._pending: dict[int, RtpPacket] = {}
        self._ssrc: int | None = None

    def push(self, packet: RtpPacket) -> None:
        if self._ssrc is None:
            self._ssrc = packet.ssrc
        elif packet.ssrc != self._ssrc:
            self.stats.bad += 1
            return
        self.stats.received += 1
        if self._expected is None:
            self._expected = packet.sequence
        if seq_diff(packet.sequence, self._expected) < 0:
            self.stats.late += 1
            return
        self._pending[packet.sequence] = packet
        self._drain()

    def _drain(self) -> None:
        while self._pending:
            packet = self._pending.pop(self._expected, None)
            if packet is not None:
                self.on_audio(g711.decode(packet.payload))
            else:
                ahead = max(seq_diff(seq, self._expected) for seq in self._pending)
                if ahead <= self.max_reorder:
                    break  # wait for the gap to fill
                self.stats.lost += 1
                self.on_audio(np.zeros(self.samples_per_packet, dtype=np.int16))
            self._expected = (self._expected + 1) & 0xFFFF


class RtpReceiverProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_audio: Callable[[np.ndarray], None]):
        self.buffer = RtpJitterBuffer(on_audio)
        self.last_packet_at: float | None = None

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            packet = RtpPacket.parse(data)
        except ValueError:
            self.buffer.stats.bad += 1
            return
        self.last_packet_at = asyncio.get_running_loop().time()
        self.buffer.push(packet)


async def open_rtp_receiver(host: str, port: int, on_audio: Callable[[np.ndarray], None]):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: RtpReceiverProtocol(on_audio), local_addr=(host, port)
    )
    return transport, protocol
