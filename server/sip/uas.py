"""Minimal SIP UAS over UDP: answers INVITE with a PCMU SDP answer,
retransmits 200 until ACK, tears the call down on BYE (either side)."""

from __future__ import annotations

import asyncio
import logging
import secrets

from server.sip.message import SipMessage, make_response
from server.sip.sdp import build_sdp, parse_sdp

log = logging.getLogger("sip.uas")

RETRANSMIT_SCHEDULE = (0.5, 1.0, 2.0)


class SipUasProtocol(asyncio.DatagramProtocol):
    def __init__(self, config, call_manager):
        self.config = config
        self.call_manager = call_manager
        self.transport: asyncio.DatagramTransport | None = None
        # call_id -> dialog state
        self.dialogs: dict[str, dict] = {}

    def connection_made(self, transport) -> None:
        self.transport = transport
        # let the call manager (e.g. the RTP idle watchdog) clean up our dialog state
        self.call_manager.on_call_ended = self.forget_dialog

    def forget_dialog(self, call_id: str) -> None:
        dialog = self.dialogs.pop(call_id, None)
        if dialog and dialog.get("task"):
            dialog["task"].cancel()

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            message = SipMessage.parse(data)
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("dropping unparseable datagram from %s: %s", addr, exc)
            return
        if not message.is_request:
            return  # we never send requests that expect responses here (BYE from us is not implemented)
        handler = {
            "INVITE": self._on_invite,
            "ACK": self._on_ack,
            "BYE": self._on_bye,
            "OPTIONS": self._on_options,
        }.get(message.method)
        if handler is None:
            self._send(make_response(message, 501, "Not Implemented"), addr)
            return
        handler(message, addr)

    # -- request handlers ------------------------------------------------

    def _on_invite(self, message: SipMessage, addr) -> None:
        call_id = message.call_id
        if not call_id:
            self._send(make_response(message, 400, "Bad Request"), addr)
            return
        dialog = self.dialogs.get(call_id)
        if dialog is not None:  # retransmitted INVITE: resend stored final response
            if dialog.get("response") is not None:
                self.transport.sendto(dialog["response"], dialog["addr"])
            return
        self.dialogs[call_id] = {"addr": addr, "response": None, "confirmed": False, "task": None}
        asyncio.get_running_loop().create_task(self._answer_invite(message, addr))

    async def _answer_invite(self, message: SipMessage, addr) -> None:
        call_id = message.call_id
        dialog = self.dialogs[call_id]
        try:
            offer = parse_sdp(message.body.decode())
            if 0 not in offer.payload_types:
                raise ValueError("peer does not offer PCMU (payload type 0)")
            rtp_port, _pipeline = await self.call_manager.start_call(call_id)
        except Exception as exc:
            log.error("INVITE for %s rejected: %s", call_id, exc)
            self._send(make_response(message, 488, "Not Acceptable Here"), addr)
            self.dialogs.pop(call_id, None)
            return
        dialog["remote_rtp"] = (offer.ip, offer.audio_port)
        response = make_response(message, 200, "OK", to_tag=secrets.token_hex(4))
        response.set("Contact", f"<sip:vad@{self.config.host}:{self.config.sip_port}>")
        response.set("Content-Type", "application/sdp")
        response.body = build_sdp(self.config.host, rtp_port).encode()
        wire = response.serialize()
        dialog["response"] = wire
        self.transport.sendto(wire, addr)
        dialog["task"] = asyncio.get_running_loop().create_task(self._retransmit_200(call_id))
        log.info("call %s answered, RTP on %s:%d", call_id, self.config.host, rtp_port)

    async def _retransmit_200(self, call_id: str) -> None:
        for delay in RETRANSMIT_SCHEDULE:
            await asyncio.sleep(delay)
            dialog = self.dialogs.get(call_id)
            if dialog is None or dialog["confirmed"]:
                return
            self.transport.sendto(dialog["response"], dialog["addr"])
        log.warning("call %s: no ACK after retransmissions, tearing down", call_id)
        self.call_manager.end_call(call_id)
        self.dialogs.pop(call_id, None)

    def _on_ack(self, message: SipMessage, addr) -> None:
        dialog = self.dialogs.get(message.call_id)
        if dialog is not None:
            dialog["confirmed"] = True

    def _on_bye(self, message: SipMessage, addr) -> None:
        self._send(make_response(message, 200, "OK"), addr)
        call_id = message.call_id
        dialog = self.dialogs.pop(call_id, None)
        if dialog is None:
            return
        if dialog.get("task"):
            dialog["task"].cancel()
        session_id = self.call_manager.end_call(call_id)
        log.info("call %s ended -> session %s", call_id, session_id)

    def _on_options(self, message: SipMessage, addr) -> None:
        self._send(make_response(message, 200, "OK"), addr)

    def _send(self, response: SipMessage, addr) -> None:
        self.transport.sendto(response.serialize(), addr)


async def start_uas(config, call_manager) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: SipUasProtocol(config, call_manager),
        local_addr=(config.host, config.sip_port),
    )
    log.info("SIP UAS listening on %s:%d/udp", config.host, config.sip_port)
    return transport
