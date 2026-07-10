"""Paced PCMU RTP sender."""

from __future__ import annotations

import asyncio
import secrets

import numpy as np

from server.rtp import g711
from server.rtp.packet import PT_PCMU, RtpPacket


class RtpSender:
    def __init__(self, transport: asyncio.DatagramTransport, dest: tuple[str, int]):
        self.transport = transport
        self.dest = dest
        self.sequence = secrets.randbelow(0x10000)
        self.timestamp = secrets.randbelow(0x100000)
        self.ssrc = secrets.randbits(32)
        self._first = True
        self.packets_sent = 0

    def send(self, pcm: np.ndarray) -> None:
        packet = RtpPacket(
            payload_type=PT_PCMU,
            sequence=self.sequence,
            timestamp=self.timestamp,
            ssrc=self.ssrc,
            payload=g711.encode(pcm),
            marker=self._first,
        )
        self.transport.sendto(packet.build(), self.dest)
        self._first = False
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp = (self.timestamp + len(pcm)) & 0xFFFFFFFF
        self.packets_sent += 1


async def open_rtp_socket(host: str, port: int) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=(host, port)
    )
    return transport
