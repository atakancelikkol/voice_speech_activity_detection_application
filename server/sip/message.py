"""Minimal SIP message parse/serialize.

Covers exactly what our own UAC/UAS pair exchanges (INVITE/ACK/BYE/OPTIONS
over UDP, full header names, single values per header except Via). Not a
general SIP stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SIP_VERSION = "SIP/2.0"


@dataclass
class SipMessage:
    method: str | None = None  # requests
    uri: str | None = None
    status: int | None = None  # responses
    reason: str | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    def get(self, name: str) -> str | None:
        lname = name.lower()
        for key, value in self.headers:
            if key.lower() == lname:
                return value
        return None

    def set(self, name: str, value: str) -> None:
        lname = name.lower()
        for i, (key, _) in enumerate(self.headers):
            if key.lower() == lname:
                self.headers[i] = (key, value)
                return
        self.headers.append((name, value))

    @property
    def call_id(self) -> str | None:
        return self.get("Call-ID")

    @property
    def cseq(self) -> tuple[int, str] | None:
        raw = self.get("CSeq")
        if not raw:
            return None
        number, _, method = raw.strip().partition(" ")
        return int(number), method.strip()

    @classmethod
    def parse(cls, data: bytes) -> "SipMessage":
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", errors="replace").split("\r\n")
        if not lines or not lines[0]:
            raise ValueError("empty SIP message")
        msg = cls(body=body)
        start = lines[0]
        if start.startswith(SIP_VERSION):
            code, _, reason = start.partition(" ")[2].partition(" ")
            msg.status = int(code)
            msg.reason = reason
        else:
            parts = start.split(" ")
            if len(parts) != 3 or parts[2] != SIP_VERSION:
                raise ValueError(f"bad SIP start line: {start!r}")
            msg.method, msg.uri = parts[0], parts[1]
        for line in lines[1:]:
            if not line:
                continue
            name, _, value = line.partition(":")
            if not _:
                raise ValueError(f"bad SIP header line: {line!r}")
            msg.headers.append((name.strip(), value.strip()))
        expected = msg.get("Content-Length")
        if expected is not None and len(msg.body) < int(expected):
            raise ValueError("truncated SIP body")
        return msg

    def serialize(self) -> bytes:
        if self.is_request:
            start = f"{self.method} {self.uri} {SIP_VERSION}"
        else:
            start = f"{SIP_VERSION} {self.status} {self.reason}"
        self.set("Content-Length", str(len(self.body)))
        lines = [start] + [f"{name}: {value}" for name, value in self.headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + self.body


def make_response(request: SipMessage, status: int, reason: str, to_tag: str | None = None) -> SipMessage:
    response = SipMessage(status=status, reason=reason)
    for name in ("Via", "From", "Call-ID", "CSeq"):
        value = request.get(name)
        if value:
            response.headers.append((name, value))
    to_value = request.get("To") or ""
    if to_tag and "tag=" not in to_value:
        to_value = f"{to_value};tag={to_tag}"
    response.headers.append(("To", to_value))
    return response
