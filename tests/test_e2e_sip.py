"""End-to-end tests over the real SIP+RTP path (localhost, in-process).

The pipeline is sample-driven (the session timeline comes from sample
counts, not wall-clock), so the WAV is streamed faster than real time to
keep the suite quick while still exercising the SIP handshake, RTP
transport, mu-law transcoding, engine fan-out and session persistence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

import client.wav_source as wav_source
import server.calls as calls
from client.main import CallController, parse_args as client_parse_args
from server.audio.wav_io import load_wav
from server.config import ServerConfig
from server.main import ServerState
from server.sip.uas import start_uas
from server.vad import registry
from server.vad.runner import SOURCE_RATE, EngineRunner

FIXTURE = Path(__file__).parent / "fixtures" / "speech.wav"
SIP_PORT = 15060  # away from the dev server's 5060
CLIENT_RTP_PORT = 41100


@pytest.fixture
def fast_streaming(monkeypatch):
    monkeypatch.setattr(wav_source, "FRAME_INTERVAL", 0.002)  # 10x real time


@pytest.fixture
async def sip_server(tmp_path):
    config = ServerConfig(
        sip_port=SIP_PORT, rtp_port_min=41000, rtp_port_max=41019, data_dir=tmp_path
    )
    state = ServerState(config)
    transport = await start_uas(config, state.call_manager)
    yield state
    state.call_manager.end_all()
    transport.close()


def make_client(wav: Path | None = None) -> CallController:
    argv = ["--server-sip-port", str(SIP_PORT), "--rtp-port", str(CLIENT_RTP_PORT)]
    if wav:
        argv += ["--wav", str(wav)]
    return CallController(client_parse_args(argv))


async def wait_for(predicate, timeout: float, message: str):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timeout: {message}")
        await asyncio.sleep(0.05)


def offline_segments(wav: Path) -> dict[str, list[tuple[float, float]]]:
    """Same audio through the same runners, without the network."""
    out: dict[str, list[tuple[float, float]]] = {}
    pcm = load_wav(wav, SOURCE_RATE)
    chunk = SOURCE_RATE * 20 // 1000
    for name, info in registry.discover().items():
        if not info.available:
            continue
        runner = EngineRunner(registry.create(info))
        for start in range(0, len(pcm), chunk):
            runner.feed(pcm[start : start + chunk])
        out[name] = [(s.start_ms, s.end_ms) for s in runner.finalize()]
    return out


class TestWavCallOverSip:
    async def test_call_produces_offline_equivalent_session(self, sip_server, fast_streaming):
        controller = make_client(FIXTURE)
        await controller.start_call("wav", str(FIXTURE))
        await wait_for(lambda: controller.state == "idle", 30.0, "call did not finish")

        sessions = sip_server.store.list_sessions()
        assert len(sessions) == 1
        session = sip_server.store.read_session(sessions[0]["id"])

        # transport was clean
        assert session["rtp"]["lost"] == 0
        assert session["rtp"]["bad"] == 0

        # duration matches the fixture
        src = load_wav(FIXTURE, SOURCE_RATE)
        expected_ms = len(src) * 1000.0 / SOURCE_RATE
        assert abs(session["duration_ms"] - expected_ms) <= 40.0  # one packet

        # the recording is the same audio (mu-law quantization aside)
        rec = load_wav(sip_server.store.audio_path(sessions[0]["id"]), SOURCE_RATE)
        n = min(len(src), len(rec))
        corr = np.corrcoef(src[:n].astype(np.float64), rec[:n].astype(np.float64))[0, 1]
        assert corr > 0.99

        # every engine that ran agrees with its own offline result
        offline = offline_segments(FIXTURE)
        assert set(session["engines"]) == set(offline)
        for name, result in session["engines"].items():
            got = [(s["start_ms"], s["end_ms"]) for s in result["segments"]]
            want = offline[name]
            assert len(got) == len(want), f"{name}: {got} vs offline {want}"
            for (gs, ge), (ws, we) in zip(got, want):
                # mu-law transcoding may shift a neural engine by a hop or two
                assert abs(gs - ws) <= 64.0, f"{name} start {gs} vs {ws}"
                assert abs(ge - we) <= 64.0, f"{name} end {ge} vs {we}"

    async def test_second_call_reuses_released_rtp_port(self, sip_server, fast_streaming):
        for _ in range(2):
            controller = make_client(FIXTURE)
            await controller.start_call("wav", str(FIXTURE))
            await wait_for(lambda: controller.state == "idle", 30.0, "call did not finish")
        assert len(sip_server.store.list_sessions()) == 2

    async def test_noisy_file_survives_the_sip_path(self, sip_server, fast_streaming):
        noisy = FIXTURE.parent / "noisy_snr5.wav"
        if not noisy.exists():
            pytest.skip("noisy fixture missing — run `make wavs`")
        controller = make_client(noisy)
        await controller.start_call("wav", str(noisy))
        await wait_for(lambda: controller.state == "idle", 30.0, "call did not finish")

        session = sip_server.store.read_session(sip_server.store.list_sessions()[0]["id"])
        assert session["rtp"]["lost"] == 0
        # heavy babble noise, but the SIP-delivered analysis still matches the
        # offline analysis of the same file for every engine
        offline = offline_segments(noisy)
        assert set(session["engines"]) == set(offline)
        for name, result in session["engines"].items():
            got = [(s["start_ms"], s["end_ms"]) for s in result["segments"]]
            assert len(got) == len(offline[name]), f"{name}: {got} vs offline {offline[name]}"
            for (gs, ge), (ws, we) in zip(got, offline[name]):
                assert abs(gs - ws) <= 64.0 and abs(ge - we) <= 64.0


class TestRtpIdleWatchdog:
    async def test_vanished_peer_gets_cleaned_up(self, sip_server, fast_streaming, monkeypatch):
        monkeypatch.setattr(calls, "RTP_IDLE_TIMEOUT_S", 0.3)
        monkeypatch.setattr(calls, "WATCHDOG_INTERVAL_S", 0.1)
        controller = make_client(FIXTURE)
        await controller.start_call("wav", str(FIXTURE))
        # kill the client mid-call without a BYE (simulates a crash)
        controller._stream_task.cancel()
        controller._uac.close()
        controller._rtp_transport.close()

        await wait_for(
            lambda: len(sip_server.store.list_sessions()) == 1,
            10.0,
            "watchdog did not finalize the abandoned call",
        )
        session = sip_server.store.read_session(sip_server.store.list_sessions()[0]["id"])
        assert session["duration_ms"] < 9000.0  # partial recording, finalized
