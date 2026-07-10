"""RTP packet parse/build (RFC 3550, fixed 12-byte header + optional CSRC)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

_HEADER = struct.Struct("!BBHII")

PT_PCMU = 0


@dataclass
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False

    @classmethod
    def parse(cls, data: bytes) -> "RtpPacket":
        if len(data) < _HEADER.size:
            raise ValueError(f"RTP packet too short: {len(data)} bytes")
        b0, b1, sequence, timestamp, ssrc = _HEADER.unpack_from(data)
        version = b0 >> 6
        if version != 2:
            raise ValueError(f"unsupported RTP version: {version}")
        csrc_count = b0 & 0x0F
        has_extension = bool(b0 & 0x10)
        offset = _HEADER.size + 4 * csrc_count
        if has_extension:
            if len(data) < offset + 4:
                raise ValueError("truncated RTP extension header")
            _, ext_words = struct.unpack_from("!HH", data, offset)
            offset += 4 + 4 * ext_words
        payload = data[offset:]
        if b0 & 0x20:  # padding: last byte holds the pad length
            payload = payload[: -payload[-1]] if payload else payload
        return cls(
            payload_type=b1 & 0x7F,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
            payload=payload,
            marker=bool(b1 & 0x80),
        )

    def build(self) -> bytes:
        b0 = 2 << 6
        b1 = (0x80 if self.marker else 0) | (self.payload_type & 0x7F)
        return _HEADER.pack(b0, b1, self.sequence & 0xFFFF, self.timestamp & 0xFFFFFFFF, self.ssrc) + self.payload


def seq_diff(a: int, b: int) -> int:
    """Shortest signed distance a-b in 16-bit sequence space."""
    return ((a - b + 0x8000) & 0xFFFF) - 0x8000
