"""Minimal SIP UAC: one dialog at a time — INVITE (with retransmission),
ACK, BYE. Provisional responses (100/180) are tolerated and ignored."""

from __future__ import annotations

import asyncio
import logging
import secrets

from server.sip.message import SIP_VERSION, SipMessage

log = logging.getLogger("sip.uac")

INVITE_RETRANSMITS = (0.5, 1.0, 2.0, 4.0)


class _UacProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.responses: asyncio.Queue[SipMessage] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            message = SipMessage.parse(data)
        except (ValueError, UnicodeDecodeError):
            return
        if not message.is_request:
            self.responses.put_nowait(message)


class SipUac:
    def __init__(self, server_host: str, server_port: int, local_ip: str = "127.0.0.1"):
        self.server = (server_host, server_port)
        self.local_ip = local_ip
        self.call_id = secrets.token_hex(8)
        self.from_tag = secrets.token_hex(4)
        self.to_header: str | None = None  # with the server's tag, once known
        self.local_port: int | None = None
        self._protocol: _UacProtocol | None = None
        self._last_ack: bytes | None = None
        self._cseq = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _UacProtocol, local_addr=(self.local_ip, 0), remote_addr=self.server
        )
        self._protocol = protocol
        self.local_port = transport.get_extra_info("sockname")[1]

    def close(self) -> None:
        if self._protocol and self._protocol.transport:
            self._protocol.transport.close()

    def _base_request(self, method: str) -> SipMessage:
        request = SipMessage(method=method, uri=f"sip:vad@{self.server[0]}:{self.server[1]}")
        request.headers = [
            ("Via", f"SIP/2.0/UDP {self.local_ip}:{self.local_port};branch=z9hG4bK{secrets.token_hex(6)}"),
            ("Max-Forwards", "70"),
            ("From", f"<sip:client@{self.local_ip}>;tag={self.from_tag}"),
            ("To", self.to_header or f"<sip:vad@{self.server[0]}>"),
            ("Call-ID", self.call_id),
            ("Contact", f"<sip:client@{self.local_ip}:{self.local_port}>"),
        ]
        return request

    async def invite(self, sdp_offer: str) -> SipMessage:
        """Send INVITE, wait for the 200, send ACK. Returns the 200."""
        self._cseq += 1
        request = self._base_request("INVITE")
        request.set("CSeq", f"{self._cseq} INVITE")
        request.set("Content-Type", "application/sdp")
        request.body = sdp_offer.encode()
        wire = request.serialize()

        final: SipMessage | None = None
        for attempt, timeout in enumerate(INVITE_RETRANSMITS):
            if attempt == 0 or final is None:
                self._protocol.transport.sendto(wire)
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    response = await asyncio.wait_for(self._protocol.responses.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if response.status and response.status >= 200:
                    final = response
                    break
                log.debug("provisional response: %s %s", response.status, response.reason)
            if final is not None:
                break
        if final is None:
            raise TimeoutError("no final response to INVITE")
        if final.status != 200:
            raise RuntimeError(f"INVITE rejected: {final.status} {final.reason}")
        self.to_header = final.get("To")
        self._send_ack()
        return final

    def _send_ack(self) -> None:
        ack = self._base_request("ACK")
        ack.set("CSeq", f"{self._cseq} ACK")
        self._last_ack = ack.serialize()
        self._protocol.transport.sendto(self._last_ack)

    async def bye(self, timeout: float = 2.0) -> None:
        self._cseq += 1
        request = self._base_request("BYE")
        request.set("CSeq", f"{self._cseq} BYE")
        self._protocol.transport.sendto(request.serialize())
        try:
            while True:
                response = await asyncio.wait_for(self._protocol.responses.get(), timeout)
                if response.cseq and response.cseq[1] == "BYE":
                    return
                if response.cseq and response.cseq[1] == "INVITE" and self._last_ack:
                    self._protocol.transport.sendto(self._last_ack)  # retransmitted 200
        except asyncio.TimeoutError:
            log.warning("no response to BYE (server may already be gone)")


def check_version() -> str:
    return SIP_VERSION
