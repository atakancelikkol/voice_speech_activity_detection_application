"""SoftphoneProxy: the main UI's one-button recorder path."""

from __future__ import annotations

import asyncio

import pytest
import uvicorn

from client.main import CallController, build_ui_app, parse_args as client_parse_args
from server.softphone import SoftphoneProxy

CLIENT_UI_PORT = 18081


@pytest.fixture
async def client_ui():
    controller = CallController(client_parse_args(["--server-sip-port", "15061", "--ui-port", str(CLIENT_UI_PORT)]))
    config = uvicorn.Config(
        build_ui_app(controller), host="127.0.0.1", port=CLIENT_UI_PORT, log_level="error"
    )
    server = uvicorn.Server(config)
    task = asyncio.get_running_loop().create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield controller
    server.should_exit = True
    await task


async def test_status_none_when_client_down():
    proxy = SoftphoneProxy("http://127.0.0.1:59999", timeout=0.3)
    assert await proxy.status() is None


async def test_start_reports_client_down_readably():
    proxy = SoftphoneProxy("http://127.0.0.1:59999", timeout=0.3)
    code, body = await proxy.start("mic")
    assert code == 503
    assert "not running" in body["detail"]


async def test_status_roundtrip(client_ui):
    proxy = SoftphoneProxy(f"http://127.0.0.1:{CLIENT_UI_PORT}")
    status = await proxy.status()
    assert status == {"state": "idle", "error": None, "level": 0.0}


async def test_start_error_passes_through(client_ui):
    proxy = SoftphoneProxy(f"http://127.0.0.1:{CLIENT_UI_PORT}")
    code, body = await proxy.start("wav", "no-such-file.wav")
    assert code == 422
    assert "no-such-file.wav" in body["detail"]


async def test_stop_when_idle_is_harmless(client_ui):
    proxy = SoftphoneProxy(f"http://127.0.0.1:{CLIENT_UI_PORT}")
    code, body = await proxy.stop()
    assert code == 200
    assert body["state"] == "idle"
