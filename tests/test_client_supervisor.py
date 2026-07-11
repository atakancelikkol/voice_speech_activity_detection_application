"""The server spawns the softphone client itself (single-flow UX). This
verifies the supervisor actually brings a real client process up and down,
and that its port only serves the redirect placeholder, not a second UI."""

from __future__ import annotations

import urllib.request

import pytest

from server.client_supervisor import ClientSupervisor

UI_PORT = 18090
SIP_PORT = 15062


@pytest.fixture
def supervisor():
    sup = ClientSupervisor(
        client_url=f"http://127.0.0.1:{UI_PORT}",
        server_sip_port=SIP_PORT,
        main_url="http://127.0.0.1:18080",
    )
    yield sup
    sup.stop()


def _get(path: str) -> tuple[int, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{UI_PORT}{path}", timeout=2) as resp:
        return resp.status, resp.read().decode()


def test_supervisor_starts_and_stops_a_real_client(supervisor):
    supervisor.start(wait_s=15.0)
    assert supervisor._is_up(), "supervisor did not bring the client up"

    status_code, _ = _get("/status")
    assert status_code == 200

    # the client's own port must not be a second app — just a signpost
    _, html = _get("/")
    assert "18080" in html and "internal service" in html

    supervisor.stop()
    # after stop the port should stop answering fairly quickly
    import time

    for _ in range(20):
        if not supervisor._is_up():
            break
        time.sleep(0.2)
    assert not supervisor._is_up(), "client process outlived supervisor.stop()"


def test_supervisor_leaves_an_already_running_client_alone(supervisor):
    supervisor.start(wait_s=15.0)
    assert supervisor._is_up()
    first_pid = supervisor._proc.pid

    # a second supervisor for the same port must not spawn a duplicate
    other = ClientSupervisor(f"http://127.0.0.1:{UI_PORT}", SIP_PORT, main_url="http://127.0.0.1:18080")
    other.start(wait_s=5.0)
    assert other._proc is None, "spawned a duplicate client on an occupied port"
    assert supervisor._proc.pid == first_pid
