"""Runs the softphone client as a child process of the server.

The client is a separate process on purpose (it is the SIP UAC placing real
calls to the server's UAS), but that is an implementation detail — the user
should only ever need the main UI on :8080. The server starts the client,
waits for its HTTP API to answer, and shuts it down on exit. If a client is
already running (e.g. started manually with `make run-client`), it is left
alone.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger("client-supervisor")


class ClientSupervisor:
    def __init__(
        self,
        client_url: str,
        server_sip_port: int,
        server_host: str = "127.0.0.1",
        main_url: str = "http://127.0.0.1:8080",
    ):
        self.client_url = client_url.rstrip("/")
        self.server_sip_port = server_sip_port
        self.server_host = server_host
        self.main_url = main_url
        parsed = urlparse(self.client_url)
        self.ui_port = parsed.port or 8081
        self._proc: subprocess.Popen | None = None

    def _is_up(self) -> bool:
        try:
            with urllib.request.urlopen(self.client_url + "/status", timeout=0.5):
                return True
        except (OSError, urllib.error.URLError):
            return False

    def start(self, wait_s: float = 8.0) -> None:
        if self._is_up():
            log.info("softphone client already running at %s — leaving it alone", self.client_url)
            return
        cmd = [
            sys.executable,
            "-m",
            "client.main",
            "--server-host",
            self.server_host,
            "--server-sip-port",
            str(self.server_sip_port),
            "--ui-port",
            str(self.ui_port),
            "--main-url",
            self.main_url,
        ]
        log.info("starting softphone client: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                log.error("softphone client exited early (code %s)", self._proc.returncode)
                self._proc = None
                return
            if self._is_up():
                log.info("softphone client ready (its own UI is on %s — you don't need it)", self.client_url)
                return
            time.sleep(0.2)
        log.warning("softphone client did not answer within %.0fs; the Record button may be disabled", wait_s)

    def stop(self) -> None:
        if self._proc is None:
            return
        log.info("stopping softphone client")
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
