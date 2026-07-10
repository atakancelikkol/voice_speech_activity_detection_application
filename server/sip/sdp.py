"""Minimal SDP: parse the peer's audio address, build our PCMU answer/offer."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class SdpInfo:
    ip: str
    audio_port: int
    payload_types: list[int]


def parse_sdp(text: str) -> SdpInfo:
    ip = None
    port = None
    payload_types: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.removeprefix("c=IN IP4 ").strip()
        elif line.startswith("m=audio "):
            parts = line.split()
            port = int(parts[1])
            payload_types = [int(pt) for pt in parts[3:] if pt.isdigit()]
    if ip is None or port is None:
        raise ValueError("SDP missing c=/m=audio line")
    return SdpInfo(ip=ip, audio_port=port, payload_types=payload_types)


def build_sdp(ip: str, audio_port: int, session_name: str = "vad") -> str:
    session_id = int(time.time())
    return "\r\n".join(
        [
            "v=0",
            f"o=- {session_id} {session_id} IN IP4 {ip}",
            f"s={session_name}",
            f"c=IN IP4 {ip}",
            "t=0 0",
            f"m=audio {audio_port} RTP/AVP 0",
            "a=rtpmap:0 PCMU/8000",
            "a=ptime:20",
        ]
    ) + "\r\n"
