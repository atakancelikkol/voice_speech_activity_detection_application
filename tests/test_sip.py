from server.sip.message import SipMessage, make_response
from server.sip.sdp import build_sdp, parse_sdp

GOLDEN_INVITE = (
    b"INVITE sip:vad@127.0.0.1:5060 SIP/2.0\r\n"
    b"Via: SIP/2.0/UDP 127.0.0.1:5070;branch=z9hG4bK776asdhds\r\n"
    b"Max-Forwards: 70\r\n"
    b"From: <sip:client@127.0.0.1>;tag=1928301774\r\n"
    b"To: <sip:vad@127.0.0.1>\r\n"
    b"Call-ID: a84b4c76e66710\r\n"
    b"CSeq: 1 INVITE\r\n"
    b"Contact: <sip:client@127.0.0.1:5070>\r\n"
    b"Content-Type: application/sdp\r\n"
    b"Content-Length: 124\r\n"
    b"\r\n"
    b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=vad\r\nc=IN IP4 127.0.0.1\r\n"
    b"t=0 0\r\nm=audio 40100 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\n"
)


def test_parse_golden_invite():
    msg = SipMessage.parse(GOLDEN_INVITE)
    assert msg.is_request and msg.method == "INVITE"
    assert msg.uri == "sip:vad@127.0.0.1:5060"
    assert msg.call_id == "a84b4c76e66710"
    assert msg.cseq == (1, "INVITE")
    assert msg.get("content-type") == "application/sdp"  # case-insensitive
    sdp = parse_sdp(msg.body.decode())
    assert (sdp.ip, sdp.audio_port, sdp.payload_types) == ("127.0.0.1", 40100, [0])


def test_serialize_parse_round_trip():
    msg = SipMessage.parse(GOLDEN_INVITE)
    again = SipMessage.parse(msg.serialize())
    assert again.method == "INVITE"
    assert again.headers[:-1] == msg.headers[:-1]  # Content-Length may be re-stated
    assert again.body == msg.body


def test_make_response_copies_dialog_headers_and_adds_to_tag():
    request = SipMessage.parse(GOLDEN_INVITE)
    response = make_response(request, 200, "OK", to_tag="srv123")
    assert not response.is_request and response.status == 200
    assert response.get("Via") == request.get("Via")
    assert response.get("From") == request.get("From")
    assert response.get("Call-ID") == request.call_id
    assert response.get("To").endswith(";tag=srv123")
    parsed = SipMessage.parse(response.serialize())
    assert parsed.status == 200 and parsed.reason == "OK"


def test_sdp_build_parse_round_trip():
    sdp = parse_sdp(build_sdp("192.168.1.10", 40002))
    assert (sdp.ip, sdp.audio_port, sdp.payload_types) == ("192.168.1.10", 40002, [0])


def test_truncated_body_rejected():
    truncated = GOLDEN_INVITE[:-30]
    try:
        SipMessage.parse(truncated)
    except ValueError as exc:
        assert "truncated" in str(exc)
    else:
        raise AssertionError("expected ValueError")
